# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/32_text.models.awdlstm.ipynb (unless otherwise specified).

__all__ = ['dropout_mask', 'RNNDropout', 'WeightDropout', 'EmbeddingDropout', 'AWD_LSTM', 'awd_lstm_lm_split',
           'awd_lstm_lm_config', 'awd_lstm_clas_split', 'awd_lstm_clas_config']

# Cell
from ...data.all import *
from ..core import *
from typing import Sequence, Generator

# Cell
def dropout_mask(
    x:Tensor, # Source tensor, output will be of the same type as `x`
    sz:Sequence[int], # Size of the dropout mask
    p:float # Dropout probability
) -> Tensor: # Multiplicative dropout mask
    "Return a dropout mask of the same type as `x`, size `sz`, with probability `p` to cancel an element."
    return x.new_empty(*sz).bernoulli_(1-p).div_(1-p)

# Cell
class RNNDropout(Module):
    "Dropout with probability `p` that is consistent on the seq_len dimension."
    def __init__(self, p:float=0.5): self.p=p

    def forward(self, x):
        if not self.training or self.p == 0.: return x
        return x * dropout_mask(x.data, (x.size(0), 1, *x.shape[2:]), self.p)

# Cell
class WeightDropout(Module):
    "A module that wraps another layer in which some weights will be replaced by 0 during training."

    def __init__(self,
        module:nn.Module, # Wrapped module
        weight_p:float, # Weight dropout probability
        layer_names:(str,Sequence[str])='weight_hh_l0' # Names of the parameters to apply dropout to
    ):
        self.module,self.weight_p,self.layer_names = module,weight_p,L(layer_names)
        for layer in self.layer_names:
            #Makes a copy of the weights of the selected layers.
            w = getattr(self.module, layer)
            delattr(self.module, layer)
            self.register_parameter(f'{layer}_raw', nn.Parameter(w.data))
            setattr(self.module, layer, w.clone())
            if isinstance(self.module, (nn.RNNBase, nn.modules.rnn.RNNBase)):
                self.module.flatten_parameters = self._do_nothing

    def _setweights(self):
        "Apply dropout to the raw weights."
        for layer in self.layer_names:
            raw_w = getattr(self, f'{layer}_raw')
            if self.training: w = F.dropout(raw_w, p=self.weight_p)
            else: w = raw_w.clone()
            setattr(self.module, layer, w)

    def forward(self, *args):
        self._setweights()
        with warnings.catch_warnings():
            # To avoid the warning that comes because the weights aren't flattened.
            warnings.simplefilter("ignore", category=UserWarning)
            return self.module(*args)

    def reset(self):
        for layer in self.layer_names:
            raw_w = getattr(self, f'{layer}_raw')
            setattr(self.module, layer, raw_w.clone())
        if hasattr(self.module, 'reset'): self.module.reset()

    def _do_nothing(self): pass

# Cell
class EmbeddingDropout(Module):
    "Apply dropout with probability `embed_p` to an embedding layer `emb`."

    def __init__(self,
        emb:nn.Embedding, # Wrapped embedding layer
        embed_p:float # Embdedding <word> dropout probability
    ):
        self.emb,self.embed_p = emb,embed_p

    def forward(self, words, scale=None):
        if self.training and self.embed_p != 0:
            size = (self.emb.weight.size(0),1)
            mask = dropout_mask(self.emb.weight.data, size, self.embed_p)
            masked_embed = self.emb.weight * mask
        else: masked_embed = self.emb.weight
        if scale: masked_embed.mul_(scale)
        return F.embedding(words, masked_embed, ifnone(self.emb.padding_idx, -1), self.emb.max_norm,
                           self.emb.norm_type, self.emb.scale_grad_by_freq, self.emb.sparse)

# Cell
class AWD_LSTM(Module):
    "AWD-LSTM inspired by https://arxiv.org/abs/1708.02182"
    initrange=0.1

    def __init__(self,
        vocab_sz:int, # Size of the vocabulary
        emb_sz:int, # Size of embedding vector
        n_hid:int, # Number of features in hidden state
        n_layers:int, # Number of LSTM layers
        pad_token:int=1, # Padding token id
        hidden_p:float=0.2, # Dropout probability for hidden state between layers
        input_p:float=0.6, # Dropout probability for LSTM stack input
        embed_p:float=0.1, # Embedding <word> dropout probabillity
        weight_p:float=0.5, # Hidden-to-hidden wight dropout probability for LSTM layers
        bidir:bool=False # If set to `True` uses bidirectional LSTM layers
    ):
        store_attr('emb_sz,n_hid,n_layers,pad_token')
        self.bs = 1
        self.n_dir = 2 if bidir else 1
        self.encoder = nn.Embedding(vocab_sz, emb_sz, padding_idx=pad_token)
        self.encoder_dp = EmbeddingDropout(self.encoder, embed_p)
        self.rnns = nn.ModuleList([self._one_rnn(emb_sz if l == 0 else n_hid, (n_hid if l != n_layers - 1 else emb_sz)//self.n_dir,
                                                 bidir, weight_p, l) for l in range(n_layers)])
        self.encoder.weight.data.uniform_(-self.initrange, self.initrange)
        self.input_dp = RNNDropout(input_p)
        self.hidden_dps = nn.ModuleList([RNNDropout(hidden_p) for l in range(n_layers)])
        self.reset()

    def forward(self, inp:Tensor, from_embeds:bool=False):
        bs,sl = inp.shape[:2] if from_embeds else inp.shape
        if bs!=self.bs: self._change_hidden(bs)

        output = self.input_dp(inp if from_embeds else self.encoder_dp(inp))
        new_hidden = []
        for l, (rnn,hid_dp) in enumerate(zip(self.rnns, self.hidden_dps)):
            output, new_h = rnn(output, self.hidden[l])
            new_hidden.append(new_h)
            if l != self.n_layers - 1: output = hid_dp(output)
        self.hidden = to_detach(new_hidden, cpu=False, gather=False)
        return output

    def _change_hidden(self, bs):
        self.hidden = [self._change_one_hidden(l, bs) for l in range(self.n_layers)]
        self.bs = bs

    def _one_rnn(self, n_in, n_out, bidir, weight_p, l):
        "Return one of the inner rnn"
        rnn = nn.LSTM(n_in, n_out, 1, batch_first=True, bidirectional=bidir)
        return WeightDropout(rnn, weight_p)

    def _one_hidden(self, l):
        "Return one hidden state"
        nh = (self.n_hid if l != self.n_layers - 1 else self.emb_sz) // self.n_dir
        return (one_param(self).new_zeros(self.n_dir, self.bs, nh), one_param(self).new_zeros(self.n_dir, self.bs, nh))

    def _change_one_hidden(self, l, bs):
        if self.bs < bs:
            nh = (self.n_hid if l != self.n_layers - 1 else self.emb_sz) // self.n_dir
            return tuple(torch.cat([h, h.new_zeros(self.n_dir, bs-self.bs, nh)], dim=1) for h in self.hidden[l])
        if self.bs > bs: return (self.hidden[l][0][:,:bs].contiguous(), self.hidden[l][1][:,:bs].contiguous())
        return self.hidden[l]

    def reset(self):
        "Reset the hidden states"
        [r.reset() for r in self.rnns if hasattr(r, 'reset')]
        self.hidden = [self._one_hidden(l) for l in range(self.n_layers)]

# Cell
def awd_lstm_lm_split(model:nn.Module) -> Generator: # Generator over parameter groups
    "Split a RNN `model` in groups for differential learning rates."
    groups = [nn.Sequential(rnn, dp) for rnn, dp in zip(model[0].rnns, model[0].hidden_dps)]
    groups = L(groups + [nn.Sequential(model[0].encoder, model[0].encoder_dp, model[1])])
    return groups.map(params)

# Cell
awd_lstm_lm_config = dict(emb_sz=400, n_hid=1152, n_layers=3, pad_token=1, bidir=False, output_p=0.1,
                          hidden_p=0.15, input_p=0.25, embed_p=0.02, weight_p=0.2, tie_weights=True, out_bias=True)

# Cell
def awd_lstm_clas_split(model:nn.Module) -> Generator: # Generator over parameter groups
    "Split a RNN `model` in groups for differential learning rates."
    groups = [nn.Sequential(model[0].module.encoder, model[0].module.encoder_dp)]
    groups += [nn.Sequential(rnn, dp) for rnn, dp in zip(model[0].module.rnns, model[0].module.hidden_dps)]
    groups = L(groups + [model[1]])
    return groups.map(params)

# Cell
awd_lstm_clas_config = dict(emb_sz=400, n_hid=1152, n_layers=3, pad_token=1, bidir=False, output_p=0.4,
                            hidden_p=0.3, input_p=0.4, embed_p=0.05, weight_p=0.5)