from timeit import default_timer as dt

import numpy as np
import torch
from addict import Dict
from torch import nn
from torch.nn import _VF
from torch.nn import functional as F
from tqdm import tqdm

from ...utils import helper
from ...utils.logger import get_logger
from .. import txtenc
from ..layers import attention, condbn, dynconv
from ..txtenc.pooling import mean_pooling
from ..txtenc import pooling
from ..txtenc import factory
from .measure import cosine_sim, l2norm

logger = get_logger()


class Cosine(nn.Module):

    def __init__(self,):
        super().__init__()
        pass

    def forward(self, img_embed, cap_embed, *args, **kwargs):
        img_embed = l2norm(img_embed, dim=1)
        cap_embed = l2norm(cap_embed, dim=1)

        return cosine_sim(img_embed, cap_embed)#.cpu()


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
