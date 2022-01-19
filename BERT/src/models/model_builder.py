import copy

import torch
import torch.nn as nn
from pytorch_transformers import BertModel, BertConfig
from torch.nn.init import xavier_uniform_

from models.decoder import TransformerDecoder
from models.encoder import Classifier, ExtTransformerEncoder
from models.optimizers import Optimizer

def build_optim(args, model, checkpoint):
    """ Build optimizer """
    if checkpoint is not None:
        optim = checkpoint['optims'][0]
        saved_optimizer_state_dict = optim.optimizer.state_dict()
        optim.optimizer.load_state_dict(saved_optimizer_state_dict)
        if args.visible_gpus != '-1':
            for state in optim.optimizer.state.values():
                for k, v in state.items():
                    if torch.is_tensor(v):
                        state[k] = v.cuda()

        if (optim.method == 'adam') and (len(optim.optimizer.state) < 1):
            raise RuntimeError(
                "Error: loaded Adam optimizer from existing model" +
                " but optimizer state is empty")

    else:
        optim = Optimizer(
            args.optim, args.lr, args.max_grad_norm,
            beta1=args.beta1, beta2=args.beta2,
            decay_method='noam',
            warmup_steps=args.warmup_steps)

    optim.set_parameters(list(model.named_parameters()))

    return optim

def build_optim_bert(args, model, checkpoint):
    """ Build optimizer """

    if checkpoint is not None:
        optim = checkpoint['optims'][0]
        saved_optimizer_state_dict = optim.optimizer.state_dict()
        optim.optimizer.load_state_dict(saved_optimizer_state_dict)
        if args.visible_gpus != '-1':
            for state in optim.optimizer.state.values():
                for k, v in state.items():
                    if torch.is_tensor(v):
                        state[k] = v.cuda()

        if (optim.method == 'adam') and (len(optim.optimizer.state) < 1):
            raise RuntimeError(
                "Error: loaded Adam optimizer from existing model" +
                " but optimizer state is empty")

    else:
        optim = Optimizer(
            args.optim, args.lr_bert, args.max_grad_norm,
            beta1=args.beta1, beta2=args.beta2,
            decay_method='noam',
            warmup_steps=args.warmup_steps_bert)

    params = [(n, p) for n, p in list(model.named_parameters()) if n.startswith('bert.model')]
    optim.set_parameters(params)


    return optim

def build_optim_dec(args, model, checkpoint):
    """ Build optimizer """

    if checkpoint is not None:
        optim = checkpoint['optims'][1]
        saved_optimizer_state_dict = optim.optimizer.state_dict()
        optim.optimizer.load_state_dict(saved_optimizer_state_dict)
        if args.visible_gpus != '-1':
            for state in optim.optimizer.state.values():
                for k, v in state.items():
                    if torch.is_tensor(v):
                        state[k] = v.cuda()

        if (optim.method == 'adam') and (len(optim.optimizer.state) < 1):
            raise RuntimeError(
                "Error: loaded Adam optimizer from existing model" +
                " but optimizer state is empty")

    else:
        optim = Optimizer(
            args.optim, args.lr_dec, args.max_grad_norm,
            beta1=args.beta1, beta2=args.beta2,
            decay_method='noam',
            warmup_steps=args.warmup_steps_dec)

    params = [(n, p) for n, p in list(model.named_parameters()) if not n.startswith('bert.model')]
    optim.set_parameters(params)


    return optim


def get_generator(vocab_size, dec_hidden_size, device):
    gen_func = nn.LogSoftmax(dim=-1)
    generator = nn.Sequential(
        nn.Linear(dec_hidden_size, vocab_size),
        gen_func
    )
    generator.to(device)

    return generator

class Bert(nn.Module):
    def __init__(self, large, temp_dir, finetune=False):
        super(Bert, self).__init__()
        if(large):
            self.model = BertModel.from_pretrained('bert-large-uncased', cache_dir=temp_dir)
        else:
            self.model = BertModel.from_pretrained('bert-base-uncased', cache_dir=temp_dir)

        self.finetune = finetune

    def forward(self, x, segs, mask):
        if(self.finetune):
            top_vec, _ = self.model(x, segs, attention_mask=mask)
        else:
            self.eval()
            with torch.no_grad():
                top_vec, _ = self.model(x, segs, attention_mask=mask)
        return top_vec


class ExtSummarizer(nn.Module):
    def __init__(self, args, device, checkpoint):
        super(ExtSummarizer, self).__init__()
        self.args = args
        self.device = device
        self.bert = Bert(args.large, args.temp_dir, args.finetune_bert)

        self.ext_layer = ExtTransformerEncoder(self.bert.model.config.hidden_size, args.ext_ff_size, args.ext_heads,
                                               args.ext_dropout, args.ext_layers)
        if (args.encoder == 'baseline'):
            bert_config = BertConfig(self.bert.model.config.vocab_size, hidden_size=args.ext_hidden_size,
                                     num_hidden_layers=args.ext_layers, num_attention_heads=args.ext_heads, intermediate_size=args.ext_ff_size)
            self.bert.model = BertModel(bert_config)
            self.ext_layer = Classifier(self.bert.model.config.hidden_size)

        if(args.max_pos>512):
            my_pos_embeddings = nn.Embedding(args.max_pos, self.bert.model.config.hidden_size)
            my_pos_embeddings.weight.data[:512] = self.bert.model.embeddings.position_embeddings.weight.data
            my_pos_embeddings.weight.data[512:] = self.bert.model.embeddings.position_embeddings.weight.data[-1][None,:].repeat(args.max_pos-512,1)
            self.bert.model.embeddings.position_embeddings = my_pos_embeddings


        if checkpoint is not None:
            self.load_state_dict(checkpoint['model'], strict=True)
        else:
            if args.param_init != 0.0:
                for p in self.ext_layer.parameters():
                    p.data.uniform_(-args.param_init, args.param_init)
            if args.param_init_glorot:
                for p in self.ext_layer.parameters():
                    if p.dim() > 1:
                        xavier_uniform_(p)

        self.to(device)

    def forward(self, src, segs, clss, mask_src, mask_cls):
        top_vec = self.bert(src, segs, mask_src)
        sents_vec = top_vec[torch.arange(top_vec.size(0)).unsqueeze(1), clss]
        sents_vec = sents_vec * mask_cls[:, :, None].float()
        sent_scores = self.ext_layer(sents_vec, mask_cls).squeeze(-1)
        return sent_scores, mask_cls


class AbsSummarizer(nn.Module):
    def __init__(self, args, device, checkpoint=None, bert_from_extractive=None):
        super(AbsSummarizer, self).__init__()
        self.args = args
        self.device = device
        self.bert = Bert(args.large, args.temp_dir, args.finetune_bert)

        if bert_from_extractive is not None:
            self.bert.model.load_state_dict(
                dict([(n[11:], p) for n, p in bert_from_extractive.items() if n.startswith('bert.model')]), strict=True)

        if (args.encoder == 'baseline'):
            bert_config = BertConfig(self.bert.model.config.vocab_size, hidden_size=args.enc_hidden_size,
                                     num_hidden_layers=args.enc_layers, num_attention_heads=8,
                                     intermediate_size=args.enc_ff_size,
                                     hidden_dropout_prob=args.enc_dropout,
                                     attention_probs_dropout_prob=args.enc_dropout)
            self.bert.model = BertModel(bert_config)

        if(args.max_pos>512):
            my_pos_embeddings = nn.Embedding(args.max_pos, self.bert.model.config.hidden_size)
            my_pos_embeddings.weight.data[:512] = self.bert.model.embeddings.position_embeddings.weight.data
            my_pos_embeddings.weight.data[512:] = self.bert.model.embeddings.position_embeddings.weight.data[-1][None,:].repeat(args.max_pos-512,1)
            self.bert.model.embeddings.position_embeddings = my_pos_embeddings
        self.vocab_size = self.bert.model.config.vocab_size
        tgt_embeddings = nn.Embedding(self.vocab_size, self.bert.model.config.hidden_size, padding_idx=0)
        if (self.args.share_emb):
            tgt_embeddings.weight = copy.deepcopy(self.bert.model.embeddings.word_embeddings.weight)

        self.decoder = TransformerDecoder(
            self.args.dec_layers,
            self.args.dec_hidden_size, heads=self.args.dec_heads,
            d_ff=self.args.dec_ff_size, dropout=self.args.dec_dropout, embeddings=tgt_embeddings)

        self.generator = get_generator(self.vocab_size, self.args.dec_hidden_size, device)
        self.generator[0].weight = self.decoder.embeddings.weight


        if checkpoint is not None:
            self.load_state_dict(checkpoint['model'], strict=True)
        else:
            for module in self.decoder.modules():
                if isinstance(module, (nn.Linear, nn.Embedding)):
                    module.weight.data.normal_(mean=0.0, std=0.02)
                elif isinstance(module, nn.LayerNorm):
                    module.bias.data.zero_()
                    module.weight.data.fill_(1.0)
                if isinstance(module, nn.Linear) and module.bias is not None:
                    module.bias.data.zero_()
            for p in self.generator.parameters():
                if p.dim() > 1:
                    xavier_uniform_(p)
                else:
                    p.data.zero_()
            if(args.use_bert_emb):
                tgt_embeddings = nn.Embedding(self.vocab_size, self.bert.model.config.hidden_size, padding_idx=0)
                tgt_embeddings.weight = copy.deepcopy(self.bert.model.embeddings.word_embeddings.weight)
                self.decoder.embeddings = tgt_embeddings
                self.generator[0].weight = self.decoder.embeddings.weight

        self.to(device)

    def forward(self, src, tgt, segs, clss, mask_src, mask_tgt, mask_cls):
        top_vec = self.bert(src, segs, mask_src)
        dec_state = self.decoder.init_decoder_state(src, top_vec)
        decoder_outputs, state = self.decoder(tgt[:, :-1], top_vec, dec_state)
        return decoder_outputs, None
        
class HybridSummarizer(nn.Module):
    def __init__(self, args, device, checkpoint = None, checkpoint_ext = None, checkpoint_abs = None):
        super(HybridSummarizer, self).__init__()
        self.args = args
        self.args
        self.device = device
        self.extractor = ExtSummarizer(args, device, checkpoint_ext)
        # self.abstractor = PGTransformers(modules, consts, options)
        self.abstractor = AbsSummarizer(args, device, checkpoint_abs)
        self.context_attn = MultiHeadedAttention(head_count = self.args.dec_heads, model_dim =self.args.dec_hidden_size, dropout=self.args.dec_dropout, need_distribution = True)

        self.v = nn.Parameter(torch.Tensor(1, self.args.dec_hidden_size * 3))
        self.bv = nn.Parameter(torch.Tensor(1))
        self.attn_lin = nn.Linear(self.args.dec_hidden_size, self.args.dec_hidden_size)
        if self.args.hybrid_loss:
            self.ext_loss_fun = torch.nn.BCELoss(reduction='none')
        if self.args.hybrid_connector:
            self.p_sen = nn.Linear(self.args.dec_hidden_size, 1)

        if checkpoint is not None:
            self.load_state_dict(checkpoint['model'], strict=True)
            print("checkpoint is loading !")
        else:
            self.attn_lin.weight.data.normal_(mean=0.0, std=0.02)

            nn.init.xavier_uniform_(self.v)
            nn.init.constant_(self.bv, 0)
            if self.args.hybrid_connector:
                for module in self.p_sen.modules():
                    # print(each)
                    if isinstance(module, (nn.Linear, nn.Embedding)):
                        module.weight.data.normal_(mean=0.0, std=0.02)
                    elif isinstance(module, nn.LayerNorm):
                        module.bias.data.zero_()
                        module.weight.data.fill_(1.0)
                    if isinstance(module, nn.Linear) and module.bias is not None:
                        module.bias.data.zero_()

            for module in self.context_attn.modules():
                if isinstance(module, (nn.Linear, nn.Embedding)):
                    module.weight.data.normal_(mean=0.0, std=0.02)
                elif isinstance(module, nn.LayerNorm):
                    module.bias.data.zero_()
                    module.weight.data.fill_(1.0)
                if isinstance(module, nn.Linear) and module.bias is not None:
                    module.bias.data.zero_()
        self.to(device)

    def forward(self, src, tgt, segs, clss, mask_src, mask_tgt, mask_cls, labels = None):

        if labels is not None and self.args.oracle:
            ext_scores = ((labels.float(), + 0.1) / 1.3) * mask_cls.float()
        else:
            if labels is None:
                with torch.no_grad():
                    ext_scores, _, sent_vec = self.extractor(src, segs, clss, mask_src, mask_cls)
            else:
                ext_scores, _, sent_vec = self.extractor(src, segs, clss, mask_src, mask_cls)
                ext_loss = self.ext_loss_fun(ext_scores, labels.float())
                ext_loss = ext_loss * mask_cls.float()

        # [batchsize * (tgt_len - 1) * hidden_size]
        # projected into the probability distribution of vocab_size from hidden state.
        decoder_outputs, encoder_state, y_emb = self.abstractor(src, tgt, segs, clss, mask_src, mask_tgt, mask_cls)
        src_pad_mask = (1 - mask_src).unsqueeze(1).repeat(1, tgt.size(1) - 1, 1)
        context_vector, attn_dist = self.context_attn(encoder_state, encoder_state, decoder_outputs, mask=src_pad_mask, type="context")
        if self.args.hybrid_connector:
            sorted_scores, sorted_scores_idx = torch.sort(ext_scores, dim=1, descending=True)
            # for top-3 select num.
            select_num = min(3, mask_cls.size(1))
            # 每个句子单独算一个值出来。
            # 这里有一堆东西，但是是把选出的前三个句子和对应评分加权，方便下边求和。
            selected_sent_vec = tuple([(sorted_scores[i][:select_num].unsqueeze(0).transpose(0,1) * sent_vec[i,tuple(sorted_scores_idx[i][:select_num])]).unsqueeze(0) for i, each in enumerate(sorted_scores_idx)])
            selected_sent_vec = torch.cat(selected_sent_vec, dim=0)
            selected_sent_vec = selected_sent_vec.sum(dim=1)
            E_sel = self.p_sen(selected_sent_vec)
            ext_scores = ext_scores * E_sel

        g = torch.sigmoid(F.linear(torch.cat([decoder_outputs, y_emb, context_vector], -1), self.v, self.bv))
        xids = src.unsqueeze(0).repeat(tgt.size(1) - 1, 1, 1).transpose(0,1)
        xids = xids * mask_tgt.unsqueeze(2)[:,:-1,:].long()

        # mask characters such as CLS um
        len0 = src.size(1)
        len0 = torch.Tensor([[len0]]).repeat(src.size(0), 1).long().to('cuda')
        clss_up = torch.cat((clss, len0), dim=1)
        sent_len = (clss_up[:, 1:] - clss) * mask_cls.long()
        for i in range(mask_cls.size(0)):
            for j in range(mask_cls.size(1)):
                if sent_len[i][j] < 0:
                    sent_len[i][j] += src.size(1)
        ext_scores_0 = ext_scores.unsqueeze(1).transpose(1,2).repeat(1,1, src.size(1))
        for i in range(clss.size(0)):
            tmp_vec = ext_scores_0[i, 0, :sent_len[i][0].int()]

            for j in range(1, clss.size(1)):
                tmp_vec = torch.cat((tmp_vec, ext_scores_0[i, j, :sent_len[i][j].int()]), dim=0)
            if i == 0:
                ext_scores_new = tmp_vec.unsqueeze(0)
            else:
                ext_scores_new = torch.cat((ext_scores_new, tmp_vec.unsqueeze(0)), dim=0)
        ext_scores_new = ext_scores_new * mask_src.float()
        attn_dist = attn_dist * (ext_scores_new + 1).unsqueeze(1)
        # Weighted sum formula.
        attn_dist = attn_dist / attn_dist.sum(dim=2).unsqueeze(2)

        ext_dist = Variable(torch.zeros(tgt.size(0), tgt.size(1) - 1, self.abstractor.bert.model.config.vocab_size).to(self.device))
        ext_vocab_prob = ext_dist.scatter_add(2, xids, (1 - g) * mask_tgt.unsqueeze(2)[:,:-1,:].float() * attn_dist) * mask_tgt.unsqueeze(2)[:,:-1,:].float()
        if self.args.hybrid_loss:
            return decoder_outputs, None, (ext_vocab_prob, g, ext_loss)
        else:
            return decoder_outputs, None, (ext_vocab_prob, g)
