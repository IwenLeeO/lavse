import torch
from addict import Dict
from torch import nn
from torch.nn import functional as F

from .layers import attention
from .loss import cosine_sim
from .txtenc.pooling import mean_pooling
from .utils.layers import l2norm
from .utils.logger import get_logger

logger = get_logger()


class CrossAttn(nn.Module):

    def __init__(
            self, device, latent_size=1024, k=8
        ):
        super().__init__()
        
        self.device = device

        self.query_conv_img = nn.Sequential(
            nn.Conv1d(
                in_channels=latent_size, 
                out_channels=latent_size//k, 
                kernel_size=1,
            ),
            nn.LeakyReLU(0.1, inplace=True),
        )
        self.key_conv_img = nn.Sequential(
            nn.Conv1d(
                in_channels=latent_size, 
                out_channels=latent_size//k, 
                kernel_size=1,
            ),
            nn.LeakyReLU(0.1, inplace=True),
        )
        self.value_conv_img = nn.Sequential(
            nn.Conv1d(
                in_channels=latent_size, 
                out_channels=latent_size, 
                kernel_size=1,
            ),
            nn.LeakyReLU(0.1, inplace=True),
        )


        self.query_conv_txt = nn.Sequential(
            nn.Conv1d(
                in_channels=latent_size, 
                out_channels=latent_size//k, 
                kernel_size=1,
            ),
            nn.LeakyReLU(0.1, inplace=True),
        )
        self.key_conv_txt = nn.Sequential(
            nn.Conv1d(
                in_channels=latent_size, 
                out_channels=latent_size//k, 
                kernel_size=1,
            ),
            nn.LeakyReLU(0.1, inplace=True),
        )
        self.value_conv_txt = nn.Sequential(
            nn.Conv1d(
                in_channels=latent_size, 
                out_channels=latent_size, 
                kernel_size=1,
            ),
            nn.LeakyReLU(0.1, inplace=True),
        )

        self.gamma_img = nn.Parameter(torch.zeros(1))
        self.gamma_txt = nn.Parameter(torch.zeros(1))
        
        self.alpha = nn.Parameter(torch.ones(1))
        self.beta = nn.Parameter(torch.zeros(1))

        self.softmax = nn.Softmax(dim=-1)

    def forward(self, img_embed, cap_embed, lens, **kwargs):
        # B, 1024, 36
        img_embed = img_embed.permute(0, 2, 1).to(self.device)
        cap_embed = cap_embed.permute(0, 2, 1).to(self.device)
        
        # B, 36, 128
        query_img = self.query_conv_img(img_embed).permute(0, 2, 1)
        # B, 128, 36
        key_img = self.key_conv_img(img_embed)
        # B, 36, 36
        # energy_img =  torch.bmm(query_img.permute(0, 2, 1), key_img)

        # B, T, 128
        query_txt = self.query_conv_img(cap_embed).permute(0, 2, 1)
        # B, 128, T
        key_txt = self.key_conv_img(cap_embed)

        # B, 1024, 36
        value_img = self.value_conv_img(img_embed)        
        # B, 1024, T
        value_txt = self.value_conv_txt(cap_embed)
        
        sims = torch.zeros(img_embed.shape[0], cap_embed.shape[0]).to(self.device)
        for i, cap in enumerate(cap_embed):
            n_words = lens[i]
            # cap: 1024, T
            cap = cap[:,:n_words]
            query_t = query_txt[i][:n_words]
            query_t = torch.stack([query_t] * len(key_img), 0)
            # energy   : B, T, 36
            energy_cross = torch.bmm(query_t, key_img) * self.alpha + self.beta
            cross_attention = self.softmax(energy_cross)
            value_t = torch.stack([value_txt[i][:,:n_words]] * len(key_img), 0)            

            img_output = torch.bmm(value_t, cross_attention)
            img_output = self.gamma_img * img_output + value_img

            img_vector = img_output.mean(-1)
            img_vector = l2norm(img_vector, dim=-1)
            cap_vector = cap[:,:n_words].mean(1)
            cap_vector = l2norm(cap_vector, dim=-1)
            sim = cosine_sim(img_vector, cap_vector.unsqueeze(0)).squeeze(-1)            
            sims[:,i] = sim

        return sims


class AdaptiveEmbedding(nn.Module):

    def __init__(
            self, device, latent_size=1024
        ):
        super().__init__()
        
        self.device = device

        # self.fc = nn.Conv1d(latent_size, latent_size*2, 1).to(device)
        self.fc = nn.Linear(latent_size, latent_size*2).to(device)
        self.bn = nn.BatchNorm1d(latent_size)

        # self.alpha = nn.Parameter(torch.ones(1))
        # self.beta = nn.Parameter(torch.zeros(1))

        self.softmax = nn.Softmax(dim=-1)
        self.norm = ClippedL2Norm()

    def forward(self, img_embed, cap_embed, lens, **kwargs):        
        '''
            img_embed: (B, 36, latent_size)
            cap_embed: (B, T, latent_size)
        '''
        # (B, 1024, T)
        cap_embed = cap_embed.permute(0, 2, 1).to(self.device)
        img_embed = img_embed.permute(0, 2, 1).to(self.device)

        cap_embed = self.norm(cap_embed)
        img_embed = self.norm(img_embed)

        sims = torch.zeros(
            img_embed.shape[0], cap_embed.shape[0]
        ).to(self.device)
        
        for i, cap in enumerate(cap_embed):
            n_words = lens[i]
            # cap: 1024, T
            # img: 1024, 36
            cap = cap[:,:n_words].mean(-1).unsqueeze(0)
            _, D = cap.shape
            params = self.fc(cap).view(D, 2)
            # D, 
            alphas = params[:,0].view(1, D, 1)
            # D, 
            betas = params[:,1].view(1, D, 1)            
            img_output = self.bn(img_embed) * alphas + betas            
            
            img_vector = img_output.mean(-1) 
            img_vector = l2norm(img_vector, dim=-1)
            cap_vector = cap
            cap_vector = l2norm(cap_vector, dim=-1)            

            sim = cosine_sim(img_vector, cap_vector).squeeze(-1)            
            sims[:,i] = sim

        return sims


class Cosine(nn.Module):

    def __init__(self, device, latent_size=1024):
        super().__init__()
        self.device = device

    def forward(self, img_embed, cap_embed, *args, **kwargs):
        return cosine_sim(img_embed, cap_embed)


class Similarity(nn.Module): 

    def __init__(self, similarity_name='cosine', **kwargs):
        super().__init__()
        self.similarity = get_similarity_object(similarity_name, **kwargs)
        logger.info(f'Created similarity: {similarity_name} with fn: {self.similarity}')
    
    def forward(self, img_embed, cap_embed, lens, shared=False):
        logger.debug(f'Similarity - img_shape: {img_embed.shape} cap_shape: {cap_embed.shape}')        
        return self.similarity(img_embed, cap_embed, lens)
    
    def forward_shared(self, img_embed, cap_embed, lens, shared_size=128):    
        """
        Computer pairwise i2t image-caption distance with locality sharding
        """
        import numpy as np
        n_im_shard = (len(img_embed)-1)//shared_size + 1
        n_cap_shard = (len(cap_embed)-1)//shared_size + 1

        logger.debug('Calculating shared similarities')

        d = torch.zeros(len(img_embed), len(cap_embed))
        for i in range(n_im_shard):
            im_start, im_end = shared_size*i, min(shared_size*(i+1), len(img_embed))
            for j in range(n_cap_shard):
                logger.info(
                    f'Shared forward batch ({i+1}/{n_im_shard},{j+1}/{n_cap_shard})'
                )
                cap_start, cap_end = shared_size*j, min(shared_size*(j+1), len(cap_embed))
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
            smooth=9, **kwargs,
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
        return((
            f'StackedAttention(task: {self.task},'
            f'i2t: {self.i2t}, '
            f'agg_function: {self.aggregate_function}, '
            f'attention: {self.attention}, '
            f'lambda_lse: {self.lambda_lse}, '
            f'agg_function: {self.agg_function}, '
            f'feature_norm: {self.feature_norm}, '
            f'lambda_lse: {self.lambda_lse}, '
            f'smooth: {self.smooth}, '
            f'kwargs: {self.kwargs})'
        ))


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


_similarities = {
    'cosine': {
        'class': Cosine,
        'args': {},
    },
    'order': None,
    'cross': {
        'class': CrossAttn,
        'args': Dict(k=8,),
    },
    'scan_i2t': {
        'class': StackedAttention,
        'args': Dict(
            i2t=True, agg_function='Mean',
            feature_norm='clipped_l2norm', 
            lambda_lse=None, smooth=4,
        ),
    },
    'adaptive': {
        'class': AdaptiveEmbedding,
        'args': Dict(),
    },
    
}


def get_similarity_object(similarity_name, **kwargs):
    settings = _similarities[similarity_name]
    args_dict = settings['args']
    args_dict.update(**kwargs)
    return settings['class'](**args_dict)


def get_sim_names():
    return _similarities.keys()
