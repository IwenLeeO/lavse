from timeit import default_timer as dt

import torch
from addict import Dict
from torch import nn
from torch.nn import _VF
from torch.nn import functional as F
from tqdm import tqdm

from .. import txtenc
from ...utils import helper
from ...utils.logger import get_logger
from ..layers import attention
from ..txtenc.pooling import mean_pooling
from .measure import cosine_sim, l2norm

logger = get_logger()


class CondBatchNorm1d(nn.Module):

    def __init__(
        self, in_features, k, cond_vector_size=None,
        normalization='batchnorm', nonlinear_proj=True,
    ):
        super().__init__()

        if normalization is None:
            self.bn = lambda x: x
        elif normalization == 'batchnorm':
            self.bn = nn.BatchNorm1d(in_features, affine=False)
        elif normalization == 'instancenorm':
            self.bn = nn.InstanceNorm1d(in_features, affine=False)

        self.nb_channels = in_features
        self.cond_vector_size = cond_vector_size

        if cond_vector_size is None:
            cond_vector_size = in_features

        if nonlinear_proj:
            self.fc_gamma = nn.Sequential(
                nn.Linear(cond_vector_size, cond_vector_size//k),
                nn.ReLU(inplace=True),
                nn.Linear(cond_vector_size//k, in_features),
            )

            self.fc_beta = nn.Sequential(
                nn.Linear(cond_vector_size, cond_vector_size//k),
                nn.ReLU(inplace=True),
                nn.Linear(cond_vector_size//k, in_features),
            )
        else:
            self.fc_gamma = nn.Sequential(
                nn.Linear(cond_vector_size, in_features),
            )

            self.fc_beta = nn.Sequential(
                nn.Linear(cond_vector_size, in_features),
            )

    def forward(self, feat_matrix, cond_vector):
        '''
        Forward conditional bachnorm using
        predicted gamma and beta returning
        the normalized input matrix

        Arguments:
            feat_matrix {torch.FloatTensor}
                -- shape: batch, features, timesteps
            cond_vector {torch.FloatTensor}
                -- shape: batch, features

        Returns:
            torch.FloatTensor
                -- shape: batch, features, timesteps
        '''

        B, D, _ = feat_matrix.shape
        Bv, Dv = cond_vector.shape

        gammas = self.fc_gamma(cond_vector).view(Bv, D, 1)
        betas  = self.fc_beta(cond_vector).view(Bv, D, 1)

        norm_feat = self.bn(feat_matrix)
        normalized = norm_feat * (gammas + 1) + betas
        return normalized


class AdaptiveEmbedding(nn.Module):

    def __init__(
            self, device, latent_size=1024, k=8, norm=False, task='t2i'
        ):
        super().__init__()

        self.device = device

        # self.fc = nn.Conv1d(latent_size, latent_size*2, 1).to(device)

        self.cbn_img = CondBatchNorm1d(latent_size, k)
        self.cbn_txt = CondBatchNorm1d(latent_size, k)

        # self.alpha = nn.Parameter(torch.ones(1))
        # self.beta = nn.Parameter(torch.zeros(1))

        self.softmax = nn.Softmax(dim=-1)
        self.norm = norm
        if norm:
            self.feature_norm = ClippedL2Norm()
        self.task = task

    def forward(self, img_embed, cap_embed, lens, **kwargs):
        '''
            img_embed: (B, 36, latent_size)
            cap_embed: (B, T, latent_size)
        '''
        # (B, 1024, T)
        cap_embed = cap_embed.permute(0, 2, 1).to(self.device)
        img_embed = img_embed.permute(0, 2, 1).to(self.device)

        # (B, 1024)
        if self.norm:
            cap_embed = self.feature_norm(cap_embed)
            img_embed = self.feature_norm(img_embed)

        sims = torch.zeros(
            img_embed.shape[0], cap_embed.shape[0]
        ).to(self.device)

        for i, cap_tensor in enumerate(cap_embed):
            # cap: 1024, T
            # img: 1024, 36

            if self.task == 't2i':
                n_words = lens[i]
                cap_repr = cap_tensor[:,:n_words].mean(-1).unsqueeze(0)

                img_output = self.cbn_img(img_embed, cap_repr)
                img_vector = img_output.mean(-1)

                img_vector = l2norm(img_vector, dim=-1)
                cap_vector = cap_repr
                cap_vector = l2norm(cap_vector, dim=-1)

                sim = cosine_sim(img_vector, cap_vector).squeeze(-1)

            if self.task == 'i2t':
                img_vectors = img_embed.mean(-1)
                cap_i_expand = cap_tensor.repeat(img_vectors.shape[0], 1, 1)
                txt_output = self.cbn_txt(cap_i_expand, img_vectors).mean(-1, keepdim=True)

                sim = cosine_similarity(
                    img_vectors.unsqueeze(2), txt_output, 1,
                )

            sims[:,i] = sim


        return sims


class AdaptiveEmbeddingI2T(nn.Module):

    def __init__(
            self, device, latent_size=1024,
            k=8, norm=False, cond_vec=False, **kwargs
        ):
        super().__init__()

        self.device = device

        # self.fc = nn.Conv1d(latent_size, latent_size*2, 1).to(device)

        self.cbn_img = CondBatchNorm1d(latent_size, k)
        self.cbn_txt = CondBatchNorm1d(latent_size, k, **kwargs)
        if cond_vec:
           self.cbn_vec = CondBatchNorm1d(latent_size, k, **kwargs)

        # self.alpha = nn.Parameter(torch.ones(1))
        # self.beta = nn.Parameter(torch.zeros(1))

        self.softmax = nn.Softmax(dim=-1)
        self.norm = norm
        if norm:
            self.feature_norm = ClippedL2Norm()

        self.cond_vec = cond_vec

    def forward(self, img_embed, cap_embed, lens, **kwargs):
        '''
            img_embed: (B, 36, latent_size)
            cap_embed: (B, T, latent_size)
        '''
        # (B, 1024, T)
        cap_embed = cap_embed.permute(0, 2, 1).to(self.device)
        img_embed = img_embed.permute(0, 2, 1).to(self.device)
        # print('cap_embed', cap_embed.shape)
        # print('img_embed', img_embed.shape)

        # (B, 1024)
        if self.norm:
            cap_embed = self.feature_norm(cap_embed)
            img_embed = self.feature_norm(img_embed)

        sims = torch.zeros(
            img_embed.shape[0], cap_embed.shape[0]
        ).to(self.device)

        img_embed = img_embed.mean(-1)

        for i, img_tensor in enumerate(img_embed):
            # cap: 1024, T
            # img: 1024, 36
            img_repr = img_tensor.unsqueeze(0)

            txt_output = self.cbn_txt(cap_embed, img_repr)
            # txt_vector = mean_pooling(txt_output.permute(0, 2, 1), lens)
            txt_vector = txt_output.max(-1)[0]
            if self.cond_vec:
                txt_vector = self.cbn_vec(txt_vector.unsqueeze(2), img_repr)
                txt_vector = txt_vector.squeeze(2)

            # print('txt vector', txt_vector.shape)
            txt_vector = l2norm(txt_vector, dim=-1)
            img_vector = img_repr
            img_vector = l2norm(img_vector, dim=-1)
            # print('txt vector -- ', txt_vector.shape)
            # print('img_vector: ', img_vector.shape)
            sim = cosine_sim(img_vector, txt_vector).squeeze(-1)
            # print('sim', sim.shape)
            sims[i,:] = sim

        return sims


class ProjRNNReducedI2T(nn.Module):

    def __init__(
            self, device, latent_size=1024,
            k=8, **kwargs
        ):
        super().__init__()

        self.device = device

        self.reduce_img = nn.Conv1d(latent_size, latent_size//k, 1)
        self.reduce_txt = nn.Conv1d(300, latent_size//k, 1)

        self.proj_text = ProjRNN(
            base_proj_channels=latent_size//k,
            rnn_input=latent_size//k,
            rnn_units=latent_size//k,
            device=device,
        )

        self.rnn = nn.GRU(
            300, latent_size, 1,
            batch_first=True, bidirectional=True,
        )

        self.sa1 = attention.SelfAttention(
            latent_size,
            nn.LeakyReLU(0.1, inplace=True),
        )

        self.txt_fc = nn.Linear(
            latent_size//k + latent_size, latent_size
        )

    def forward(self, img_embed, cap_embed, lens, **kwargs):
        '''
            img_embed: (B, 36, latent_size)
            cap_embed: (B, T, latent_size)
        '''
        # (B, 1024, T)
        # cap_embed = cap_embed.permute(0, 2, 1).to(self.device)

        txt_embed, _ = self.rnn(cap_embed)
        txt_embed = (
            txt_embed[:,:,:txt_embed.size(2)//2] + txt_embed[:,:,txt_embed.size(2)//2:]
        )/2
        txt_embed = self.sa1(txt_embed.permute(0, 2, 1))
        txt_embed = mean_pooling(txt_embed.permute(0, 2, 1), lens)

        img_embed = img_embed.permute(0, 2, 1).to(self.device)
        img_embed_global = img_embed.mean(-1)

        cap_embed = cap_embed.permute(0, 2, 1).to(self.device)

        sims = torch.zeros(
            img_embed.shape[0], cap_embed.shape[0]
        ).to(self.device)

        cap_reduced = self.reduce_txt(cap_embed)
        img_reduced = self.reduce_img(img_embed)

        img_global_vec = img_reduced.mean(-1)

        for i, img_vector in enumerate(img_global_vec):
            # cap: 1024, T
            # img: 1024, 36
            img_repr = img_vector.unsqueeze(0)

            txt_output = self.proj_text(
                cap_reduced.permute(0, 2, 1), img_vector
            ).permute(0, 2, 1)
            txt_vector = txt_output.max(-1)[0]
            txt_vector = self.txt_fc(
                torch.cat([txt_embed, txt_vector], dim=1)
            )

            txt_vector = l2norm(txt_vector, dim=-1)
            img_vector = img_embed_global[i].unsqueeze(0)
            img_vector = l2norm(img_vector, dim=-1)
            sim = cosine_sim(img_vector, txt_vector).squeeze(-1)
            sims[i,:] = sim

        return sims


class MixedConv(nn.Module):

    def __init__(
        self, base_proj_channels,
        in_channels, out_channels,
        kernel_size, groups,
    ):
        super().__init__()

        self.conv1p = ProjConv1d(
            base_proj_channels=base_proj_channels,
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            groups=groups,
        )
        self.conv1a = nn.Conv1d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            groups=groups,
        )

        self.batch_norm = nn.BatchNorm1d(out_channels*2)

        self.conv1 = nn.Conv1d(
            in_channels=in_channels*2,
            out_channels=out_channels,
            kernel_size=1,
        )

        # self.sa1 = attention.SelfAttention(
        #     out_channels,
        #     nn.LeakyReLU(0.1, inplace=True),
        # )

    def forward(self, x, base_feat):
        proj_x = self.conv1p(x, base_feat)
        for_x = self.conv1a(x)
        x = torch.cat([proj_x, for_x], 1)
        x = self.batch_norm(x)
        x = nn.ReLU(inplace=True)(x)
        x = self.conv1(x)
        # x_sa = self.sa1(x)
        return x


class ProjConvReducedI2T(nn.Module):

    def __init__(
            self, device, latent_size=1024,
            k=8, **kwargs
        ):
        super().__init__()

        self.device = device

        self.reduce_img = nn.Conv1d(latent_size, latent_size//k, 1)
        self.reduce_txt = nn.Conv1d(300, latent_size//k, 1)

        self.mixed_a = MixedConv(
            base_proj_channels=latent_size//k,
            in_channels=latent_size//k,
            out_channels=latent_size//k,
            kernel_size=2,
            groups=2,
        )

        self.mixed_b = MixedConv(
            base_proj_channels=latent_size//k,
            in_channels=latent_size//k,
            out_channels=latent_size//k,
            kernel_size=2,
            groups=2,
        )

        # self.mixed_c = MixedConv(
        #     base_proj_channels=latent_size//k,
        #     in_channels=latent_size//k,
        #     out_channels=latent_size//k,
        #     kernel_size=2,
        #     groups=2,
        # )

        # self.rnn = nn.GRU(
        #     latent_size//k, latent_size, 1,
        #     batch_first=True,
        #     bidirectional=True
        # )

        # self.sa1 = attention.SelfAttention(
        #     latent_size, nn.LeakyReLU(0.1)
        # )


        self.txt_fc = nn.Conv1d(
            latent_size//k, latent_size, 1
        )

    def forward(self, img_embed, cap_embed, lens, **kwargs):
        '''
            img_embed: (B, 36, latent_size)
            cap_embed: (B, T, latent_size)
        '''
        # (B, 1024, T)
        # cap_embed = cap_embed.permute(0, 2, 1).to(self.device)

        cap_embed = cap_embed.permute(0, 2, 1).to(self.device)
        img_embed = img_embed.permute(0, 2, 1).to(self.device)
        img_embed_global = img_embed.mean(-1)

        sims = torch.zeros(
            img_embed.shape[0], cap_embed.shape[0]
        ).to(self.device)

        cap_reduced = self.reduce_txt(cap_embed)
        img_reduced = self.reduce_img(img_embed)

        img_global_vec = img_reduced.mean(-1)

        for i, img_vector in enumerate(img_global_vec):
            # cap: 1024, T
            # img: 1024, 36
            img_repr = img_vector.unsqueeze(0)

            x = self.mixed_a(cap_reduced, img_repr)
            x = self.mixed_b(x, img_repr)
            # x = self.mixed_c(x, img_repr)
            # x, _ = self.rnn(x.permute(0, 2, 1))
            # b, t, d = x.shape
            # x = x.view(b, t, 2, d//2).mean(-2)
            x = self.txt_fc(x)
            txt_vector = x.max(-1)[0]

            txt_vector = l2norm(txt_vector, dim=-1)
            img_vector = img_embed_global[i].unsqueeze(0)
            img_vector = l2norm(img_vector, dim=-1)
            sim = cosine_sim(img_vector, txt_vector).squeeze(-1)
            sims[i,:] = sim

        return sims


class ProjConv1d(nn.Module):

    def __init__(
        self, base_proj_channels, in_channels, out_channels,
        kernel_size, padding=0, groups=1, proj_bias=True,
        weightnorm='batchnorm',
    ):
        super().__init__()

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self.padding = padding
        self.proj_bias = proj_bias
        self.base_proj_channels = base_proj_channels
        self.groups = groups

        if weightnorm == None:
            weightnorm = False

        self.weightnorm = weightnorm

        self.kernel = nn.Linear(
            base_proj_channels,
            (in_channels * out_channels * kernel_size) // groups
        )
        if weightnorm == 'batchnorm':
            self.weight_norm = nn.BatchNorm1d(out_channels)
        elif weightnorm == 'softmax':
            self.weight_norm = nn.Softmax(dim=-1)
        elif weightnorm == 'l2':
            self.weight_norm = lambda x: l2norm(x, dim=1)
        if proj_bias:
            self.bias = nn.Linear(base_proj_channels, out_channels)

        print(self.kernel)

    def forward(self, input, base_proj):
        kernel = self.kernel(base_proj)
        kernel = kernel.view(
            self.out_channels,
            self.in_channels // self.groups,
            self.kernel_size
        )
        if self.weightnorm:
            kernel = self.weight_norm(
                kernel.permute(1, 0, 2)
            ).permute(1, 0, 2)
        if self.proj_bias:
            bias = self.bias(base_proj)
            bias = bias.view(self.out_channels)

        return F.conv1d(
            input, kernel, bias=bias, stride=1,
            padding=self.padding, dilation=1,
            groups=self.groups,
        )


class ProjRNN(nn.Module):

    def __init__(
        self, base_proj_channels, rnn_input, rnn_units, device
    ):
        super().__init__()

        self.base_proj_channels = base_proj_channels
        self.rnn_units = rnn_units
        self.weights_dim = rnn_units * 3
        self.device = device
        self.rnn_input = rnn_input

        self.weight_ih = nn.Linear(base_proj_channels, self.weights_dim * rnn_input)
        self.weight_hh = nn.Linear(base_proj_channels, self.weights_dim * rnn_units)
        self.bias_ih = nn.Linear(base_proj_channels, self.weights_dim)
        self.bias_hh = nn.Linear(base_proj_channels, self.weights_dim)

        print(self.weight_ih)
        print(self.weight_hh)
        print(self.bias_ih)
        print(self.bias_hh)

    def forward(self, input, base_proj):
        b, t, d = input.shape

        weight_ih = self.weight_ih(base_proj)
        weight_ih = weight_ih.view(
            self.weights_dim, self.rnn_input
        )
        weight_hh = self.weight_hh(base_proj)
        weight_hh = weight_hh.view(
            self.weights_dim, self.rnn_units
        )
        bias_ih = self.bias_ih(base_proj).view(-1)
        bias_hh = self.bias_hh(base_proj).view(-1)

        hx = torch.zeros(b,  self.rnn_units).to(self.device)

        outputs = []
        for i in range(t):
            input_vec = input[:,i]
            out = _VF.gru_cell(
                input_vec, hx,
                weight_ih, weight_hh,
                bias_ih, bias_hh,
            )
            outputs.append(out)
        outputs = torch.stack(outputs, 0)
        outputs = outputs.permute(1, 0, 2)
        return outputs


class Cosine(nn.Module):

    def __init__(self, device, latent_size=1024):
        super().__init__()
        self.device = device

    def forward(self, img_embed, cap_embed, *args, **kwargs):
        img_embed = l2norm(img_embed, dim=1)
        cap_embed = l2norm(cap_embed, dim=1)
        return cosine_sim(img_embed, cap_embed)


class Similarity(nn.Module):

    def __init__(self, device, similarity_object, **kwargs):
        super().__init__()
        self.device = device
        self.similarity = similarity_object
        # self.similarity = factory.get_similarity_object(similarity_name, device=device, **kwargs)
        logger.info(f'Created similarity: {similarity_object}')
        self.set_master_()

    def set_master_(self, is_master=True):
        self.master = is_master

    def forward(self, img_embed, cap_embed, lens, shared=False):
        logger.debug((
            f'Similarity - img_shape: {img_embed.shape} '
            'cap_shape: {cap_embed.shape}'
        ))
        return self.similarity(img_embed, cap_embed, lens)

    def forward_shared(self, img_embed, cap_embed, lens, shared_size=128):
        """
        Computer pairwise i2t image-caption distance with locality sharding
        """

        img_embed = img_embed.to(self.device)
        cap_embed = cap_embed.to(self.device)

        n_im_shard = (len(img_embed)-1)//shared_size + 1
        n_cap_shard = (len(cap_embed)-1)//shared_size + 1

        logger.debug('Calculating shared similarities')

        pbar_fn = lambda x: range(x)
        if self.master:
            pbar_fn = lambda x: tqdm(
                range(x), total=x, 
                desc='Test  ', 
                leave=False,
            )

        d = torch.zeros(len(img_embed), len(cap_embed)).cpu()
        for i in pbar_fn(n_im_shard):
            im_start = shared_size*i
            im_end = min(shared_size*(i+1), len(img_embed))
            for j in range(n_cap_shard):
                cap_start = shared_size*j
                cap_end = min(shared_size*(j+1), len(cap_embed))
                im = img_embed[im_start:im_end]
                s = cap_embed[cap_start:cap_end]
                l = lens[cap_start:cap_end]
                sim = self.forward(im, s, l)
                d[im_start:im_end, cap_start:cap_end] = sim                

        logger.debug('Done computing shared similarities.')
        return d


class LogSumExp(nn.Module):
    def __init__(self, lambda_lse):
        self.lambda_lse = lambda_lse

    def forward(self, x):
        x.mul_(self.lambda_lse).exp_()
        x = x.sum(dim=1, keepdim=True)
        x = torch.log(x)/self.lambda_lse
        return x


class ClippedL2Norm(nn.Module):
    def __init__(self, ):
        super().__init__()
        self.leaky = nn.LeakyReLU(0.1)

    def forward(self, x):
        return l2norm(self.leaky(x), 2)


class StackedAttention(nn.Module):

    def __init__(
        self, i2t=True, agg_function='Mean',
        feature_norm='softmax', lambda_lse=None,
        smooth=4, **kwargs,
    ):
        super().__init__()
        self.i2t = i2t
        self.lambda_lse = lambda_lse
        self.agg_function = agg_function
        self.feature_norm = feature_norm
        self.lambda_lse = lambda_lse
        self.smooth = smooth
        self.kwargs = kwargs

        self.attention = Attention(
            smooth=smooth, feature_norm=feature_norm,
        )

        if agg_function == 'LogSumExp':
            self.aggregate_function = LogSumExp(lambda_lse)
        elif agg_function == 'Max':
            self.aggregate_function = lambda x: x.max(dim=1, keepdim=True)[0]
        elif agg_function == 'Sum':
            self.aggregate_function = lambda x: x.sum(dim=1, keepdim=True)
        elif agg_function == 'Mean':
            self.aggregate_function = lambda x: x.mean(dim=1, keepdim=True)
        else:
            raise ValueError("unknown aggfunc: {}".format(agg_function))

        self.task = 'i2t' if i2t else 't2i'

    def forward(self, images, captions, cap_lens, ):
        """
        Images: (n_image, n_regions, d) matrix of images
        Captions: (n_caption, max_n_word, d) matrix of captions
        CapLens: (n_caption) array of caption lengths
        """
        similarities = []
        n_image = images.size(0)
        n_caption = captions.size(0)

        for i in range(n_caption):
            # Get the i-th text description
            n_word = cap_lens[i]
            cap_i = captions[i, :n_word, :].unsqueeze(0).contiguous()
            # --> (n_image, n_word, d)
            cap_i_expand = cap_i.repeat(n_image, 1, 1)
            """
                word(query): (n_image, n_word, d)
                image(context): (n_image, n_regions, d)
                weiContext: (n_image, n_word, d) or (n_image, n_region, d)
                attn: (n_image, n_region, n_word)
            """
            emb_a = cap_i_expand
            emb_b = images
            if self.i2t:
                emb_a = images
                emb_b = cap_i_expand

            weiContext, attn = self.attention(emb_a, emb_b)
            emb_a = emb_a.contiguous()
            weiContext = weiContext.contiguous()
            # (n_image, n_word)
            row_sim = cosine_similarity(emb_a, weiContext, dim=2)
            row_sim = self.aggregate_function(row_sim)
            similarities.append(row_sim)

        # (n_image, n_caption)
        similarities = torch.cat(similarities, 1)

        return similarities

    def __repr__(self, ):
        return (
            f'StackedAttention(task: {self.task},'
            f'i2t: {self.i2t}, '
            f'attention: {self.attention}, '
            f'lambda_lse: {self.lambda_lse}, '
            f'agg_function: {self.agg_function}, '
            f'feature_norm: {self.feature_norm}, '
            f'lambda_lse: {self.lambda_lse}, '
            f'smooth: {self.smooth}, '
            f'kwargs: {self.kwargs})'
        )


def attn_softmax(attn):
    batch_size, sourceL, queryL = attn.shape
    attn = attn.view(batch_size*sourceL, queryL)
    attn = nn.Softmax(dim=-1)(attn)
    # --> (batch, sourceL, queryL)
    attn = attn.view(batch_size, sourceL, queryL)
    return attn


class Attention(nn.Module):

    def __init__(self, smooth, feature_norm='softmax'):
        super().__init__()
        self.smooth = smooth
        self.feature_norm = feature_norm

        if feature_norm == "softmax":
            self.normalize_attn = attn_softmax
        # elif feature_norm == "l2norm":
        #     attn = lambda x: l2norm(x, 2)
        elif feature_norm == "clipped_l2norm":
            self.normalize_attn = ClippedL2Norm()
        # elif feature_norm == "l1norm":
        #     attn = l1norm_d(attn, 2)
        # elif feature_norm == "clipped_l1norm":
        #     attn = nn.LeakyReLU(0.1)(attn)
        #     attn = l1norm_d(attn, 2)
        elif feature_norm == "clipped":
            self.normalize_attn = lambda x: nn.LeakyReLU(0.1)(x)
        elif feature_norm == "no_norm":
            self.normalize_attn = lambda x: x
        else:
            raise ValueError("unknown first norm type:", feature_norm)

    def forward(self, query, context, ):
        batch_size_q, queryL = query.size(0), query.size(1)
        batch_size, sourceL = context.size(0), context.size(1)

         # Get attention
        # --> (batch, d, queryL)
        queryT = torch.transpose(query, 1, 2)

        # (batch, sourceL, d)(batch, d, queryL)
        # --> (batch, sourceL, queryL)
        attn = torch.bmm(context, queryT)
        attn = self.normalize_attn(attn)
        # --> (batch, queryL, sourceL)
        attn = torch.transpose(attn, 1, 2).contiguous()
        # --> (batch*queryL, sourceL)
        attn = attn.view(batch_size*queryL, sourceL)
        attn = nn.Softmax(dim=-1)(attn*self.smooth)
        # --> (batch, queryL, sourceL)
        attn = attn.view(batch_size, queryL, sourceL)
        # --> (batch, sourceL, queryL)
        attnT = torch.transpose(attn, 1, 2).contiguous()

        # --> (batch, d, sourceL)
        contextT = torch.transpose(context, 1, 2)
        # (batch x d x sourceL)(batch x sourceL x queryL)
        # --> (batch, d, queryL)
        weightedContext = torch.bmm(contextT, attnT)
        # --> (batch, queryL, d)
        weightedContext = torch.transpose(weightedContext, 1, 2)

        return weightedContext, attnT


def cosine_similarity(x1, x2, dim=1, eps=1e-8):
    """Returns cosine similarity between x1 and x2, computed along dim."""
    w12 = torch.sum(x1 * x2, dim)
    w1 = torch.norm(x1, 2, dim)
    w2 = torch.norm(x2, 2, dim)
    return (w12 / (w1 * w2).clamp(min=eps)).squeeze()
