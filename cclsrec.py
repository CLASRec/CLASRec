# -*- coding: utf-8 -*-
import random
import torch
from torch import nn

from model.abstract_recommender import SequentialRecommender
from layers import TransformerEncoder
from layers import TransformerEncoder_free
from model.loss import BPRLoss


class CCLSRec(SequentialRecommender):
    r"""
    SASRec is the first sequential recommender based on self-attentive mechanism.

    NOTE:
        In the author's implementation, the Point-Wise Feed-Forward Network (PFFN) is implemented
        by CNN with 1x1 kernel. In this implementation, we follows the original BERT implementation
        using Fully Connected Layer to implement the PFFN.
    """

    def __init__(self, config, dataset):
        super(CCLSRec, self).__init__(config, dataset)

        # load parameters info
        self.n_layers = config['n_layers']
        self.n_heads = config['n_heads']
        self.hidden_size = config['hidden_size']  # same as embedding_size
        self.inner_size = config['inner_size']  # the dimensionality in feed-forward layer
        self.hidden_dropout_prob = config['hidden_dropout_prob']
        self.attn_dropout_prob = config['attn_dropout_prob']
        self.hidden_act = config['hidden_act']
        self.layer_norm_eps = config['layer_norm_eps']

        self.initializer_range = config['initializer_range']
        self.loss_type = config['loss_type']

        self.batch_size = config['train_batch_size']
        self.lmd = config['lmd']
        self.niu = config['niu']
        self.tau = config['tau']
        self.tao = config['tao']
        self.sim = config['sim']
        self.mask_ratio = config['mask_ratio']
        self.mask_token = self.n_items
        self.mask_item_length = int(self.mask_ratio * self.max_seq_length)

        # define layers and loss
        self.item_embedding = nn.Embedding(self.n_items, self.hidden_size, padding_idx=0)
        self.position_embedding = nn.Embedding(self.max_seq_length, self.hidden_size)
        self.trm_encoder = TransformerEncoder(
            n_layers=self.n_layers,
            n_heads=self.n_heads,
            hidden_size=self.hidden_size,
            inner_size=self.inner_size,
            hidden_dropout_prob=self.hidden_dropout_prob,
            attn_dropout_prob=self.attn_dropout_prob,
            hidden_act=self.hidden_act,
            layer_norm_eps=self.layer_norm_eps
        )
        self.trm_encoder_free = TransformerEncoder_free(
            n_layers=self.n_layers,
            n_heads=self.n_heads,
            hidden_size=self.hidden_size,
            inner_size=self.inner_size,
            hidden_dropout_prob=self.hidden_dropout_prob,
            attn_dropout_prob=self.attn_dropout_prob,
            hidden_act=self.hidden_act,
            layer_norm_eps=self.layer_norm_eps
        )

        self.LayerNorm = nn.LayerNorm(self.hidden_size, eps=self.layer_norm_eps)
        self.dropout = nn.Dropout(self.hidden_dropout_prob)

        if self.loss_type == 'BPR':
            self.loss_fct = nn.BCEWithLogitsLoss()
        elif self.loss_type == 'CE':
            self.loss_fct = nn.CrossEntropyLoss()  # CE？
        else:
            raise NotImplementedError("Make sure 'loss_type' in ['BPR', 'CE']!")

        self.mask_default = self.mask_correlated_samples(batch_size=self.batch_size)
        self.nce_fct = nn.CrossEntropyLoss()

        # parameters initialization
        self.apply(self._init_weights)

    def _init_weights(self, module):
        """ Initialize the weights """
        if isinstance(module, (nn.Linear, nn.Embedding)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.initializer_range)
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        if isinstance(module, nn.Linear) and module.bias is not None:
            module.bias.data.zero_()

    def get_attention_mask(self, item_seq):
        """Generate left-to-right uni-directional attention mask for multi-head attention."""
        attention_mask = (item_seq > 0).long()
        extended_attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)  # torch.int64
        # mask for left-to-right unidirectional
        max_len = attention_mask.size(-1)
        attn_shape = (1, max_len, max_len)
        subsequent_mask = torch.triu(torch.ones(attn_shape), diagonal=1)  # torch.uint8
        subsequent_mask = (subsequent_mask == 0).unsqueeze(1)
        subsequent_mask = subsequent_mask.long().to(item_seq.device)

        extended_attention_mask = extended_attention_mask * subsequent_mask
        extended_attention_mask = extended_attention_mask.to(dtype=next(self.parameters()).dtype)  # fp16 compatibility
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0
        return extended_attention_mask

    def forward(self, item_seq, item_seq_len):
        position_ids = torch.arange(item_seq.size(1), dtype=torch.long, device=item_seq.device)
        position_ids = position_ids.unsqueeze(0).expand_as(item_seq)
        position_embedding = self.position_embedding(position_ids)

        item_emb = self.item_embedding(item_seq)
        input_emb = item_emb + position_embedding
        input_emb = self.LayerNorm(input_emb)
        input_emb = self.dropout(input_emb)

        extended_attention_mask = self.get_attention_mask(item_seq)

        trm_output = self.trm_encoder(input_emb, extended_attention_mask, output_all_encoded_layers=True)
        output = trm_output[-1]
        output = self.gather_indexes(output, item_seq_len - 1)
        return output  # [B H]

    def forward_1(self, item_seq, item_seq_len):
        position_ids = torch.arange(item_seq.size(1), dtype=torch.long, device=item_seq.device)
        position_ids = position_ids.unsqueeze(0).expand_as(item_seq)
        position_embedding = self.position_embedding(position_ids)

        item_emb = self.item_embedding(item_seq)
        input_emb = item_emb + position_embedding
        input_emb = self.LayerNorm(input_emb)
        # input_emb = self.dropout(input_emb)

        extended_attention_mask = self.get_attention_mask(item_seq)

        trm_output = self.trm_encoder_free(input_emb, extended_attention_mask, output_all_encoded_layers=True)
        output = trm_output[-1]
        output = self.gather_indexes(output, item_seq_len - 1)
        return output  # [B H]

    def calculate_loss(self, interaction):
        item_seq = interaction[self.ITEM_SEQ]
        item_seq_len = interaction[self.ITEM_SEQ_LEN]
        seq_output = self.forward(item_seq, item_seq_len)
        pos_items = interaction[self.POS_ITEM_ID]
        seq1 = self.reconstruct_train_data1(item_seq)
        seq2 = self.reconstruct_train_data2(item_seq)
        if self.loss_type == 'BPR':
            neg_items = interaction[self.NEG_ITEM_ID]
            pos_items_emb = self.item_embedding(pos_items)
            neg_items_emb = self.item_embedding(neg_items)
            pos_score = torch.sum(seq_output * pos_items_emb, dim=-1)  # [B] pos_logites
            neg_score = torch.sum(seq_output * neg_items_emb, dim=-1)  # [B] neg_logites
            # loss = self.loss_fct(pos_score, neg_score)
            loss = self.loss_fct(pos_score, pos_items_emb)  # 还是pos_score,pos_item_emb
            loss += self.loss_fct(neg_score, neg_items_emb)
            return loss
        else:  # self.loss_type = 'CE'
            test_item_emb = self.item_embedding.weight
            x1=torch.norm(seq_output, p=2)
            x2=torch.norm(test_item_emb, p=2)
            # print(x1,x2)
            seq_output = seq_output/x1
            test_item_emb = test_item_emb/x2
            logits = torch.matmul(seq_output, test_item_emb.transpose(0, 1))
            logits = logits /self.tao
            loss = self.loss_fct(logits, pos_items)
            seq_output1 = self.forward(item_seq, item_seq_len)
            seq1_out = self.forward_1(seq1, item_seq_len)
            seq2_out = self.forward_1(seq2, item_seq_len)
            seq_output2 = self.forward(item_seq, item_seq_len)
            guid_logits, guid_labels = self.info_nce(seq_output1, seq_output, temp=self.tau,
                                                   batch_size=item_seq.shape[0], sim=self.sim)
            nce_loss = self.nce_fct(guid_logits, guid_labels)
            with torch.no_grad():
                guid_alignment, guid_uniformity = self.decompose(seq_output1, seq_output2, seq_output,
                                                                 batch_size=item_seq_len.shape[0])
            free_logits, free_labels = self.info_nce(seq1_out, seq2_out, temp=self.tau,
                                                     batch_size=seq1.shape[0], sim=self.sim)
            with torch.no_grad():
                free_alignment, free_uniformity = self.decompose(seq1_out, seq2_out, seq_output,
                                                                 batch_size=item_seq_len.shape[0])
            cl_loss = self.nce_fct(free_logits, free_labels)
            return loss + self.lmd * nce_loss + self.niu * cl_loss, guid_alignment, guid_uniformity, free_alignment, free_uniformity

    def predict(self, interaction):
        item_seq = interaction[self.ITEM_SEQ]
        item_seq_len = interaction[self.ITEM_SEQ_LEN]
        test_item = interaction[self.ITEM_ID]
        seq_output = self.forward(item_seq, item_seq_len)
        test_item_emb = self.item_embedding(test_item)
        x1 = torch.norm(seq_output, p=2)
        x2 = torch.norm(test_item_emb, p=2)
        seq_output = seq_output / x1
        test_item_emb = test_item_emb / x2
        scores = torch.mul(seq_output, test_item_emb).sum(dim=1)  # [B]
        scores = scores /self.tao
        return scores

    def full_sort_predict(self, interaction):
        item_seq = interaction[self.ITEM_SEQ]
        item_seq_len = interaction[self.ITEM_SEQ_LEN]
        seq_output = self.forward(item_seq, item_seq_len)
        test_items_emb = self.item_embedding.weight
        x1 = torch.norm(seq_output, p=2)
        x2 = torch.norm(test_items_emb, p=2)
        seq_output = seq_output / x1
        test_items_emb = test_items_emb / x2
        scores = torch.matmul(seq_output, test_items_emb.transpose(0, 1))  # [B n_items]
        scores = scores /self.tao
        return scores

    def mask_correlated_samples(self, batch_size):
        N = 2 * batch_size
        mask = torch.ones((N, N), dtype=bool)
        mask = mask.fill_diagonal_(0)
        for i in range(batch_size):
            mask[i, batch_size + i] = 0
            mask[batch_size + i, i] = 0
        return mask

    def info_nce(self, z_i, z_j, temp, batch_size, sim='dot'):
        """
        We do not sample negative examples explicitly.
        Instead, given a positive pair, similar to (Chen et al., 2017), we treat the other 2(N − 1) augmented examples within a minibatch as negative examples.
        """
        N = 2 * batch_size

        z = torch.cat((z_i, z_j), dim=0)

        if sim == 'cos':
            sim = nn.functional.cosine_similarity(z.unsqueeze(1), z.unsqueeze(0), dim=2) / temp
        elif sim == 'dot':
            sim = torch.mm(z, z.T) / temp

        sim_i_j = torch.diag(sim, batch_size)
        sim_j_i = torch.diag(sim, -batch_size)

        positive_samples = torch.cat((sim_i_j, sim_j_i), dim=0).reshape(N, 1)
        if batch_size != self.batch_size:
            mask = self.mask_correlated_samples(batch_size)
        else:
            mask = self.mask_default
        negative_samples = sim[mask].reshape(N, -1)

        labels = torch.zeros(N).to(positive_samples.device).long()
        logits = torch.cat((positive_samples, negative_samples), dim=1)
        return logits, labels

    def reconstruct_train_data1(self, item_seq):
        """
        Random sample sequence for training.
        """
        device = item_seq.device
        batch_size = item_seq.size(0)

        sequence_instances = item_seq.cpu().numpy().tolist()
        masked_item_sequence = []
        for instance in sequence_instances:
            masked_sequence = []
            for index_id, item in enumerate(instance):
                prob = random.random()
                if prob > self.mask_ratio:
                    masked_sequence.append(item)
            masked_item_sequence.append(masked_sequence)
        masked_item_sequence = torch.tensor(masked_item_sequence, dtype=torch.long, device=device).view(batch_size, -1)
        return masked_item_sequence

    def reconstruct_train_data2(self, item_seq):
        """
        Random sample sequence for training.
        """
        device = item_seq.device
        batch_size = item_seq.size(0)

        sequence_instances = item_seq.cpu().numpy().tolist()
        masked_item_sequence = []
        for instance in sequence_instances:
            masked_sequence = []
            for index_id, item in enumerate(instance):
                prob = random.random()
                if prob < self.mask_ratio:
                    masked_sequence.append(item)
            masked_item_sequence.append(masked_sequence)
        masked_item_sequence = torch.tensor(masked_item_sequence, dtype=torch.long, device=device).view(batch_size, -1)
        return masked_item_sequence

    def decompose(self, z_i, z_j, origin_z, batch_size):
        """
        We do not sample negative examples explicitly.
        Instead, given a positive pair, similar to (Chen et al., 2017), we treat the other 2(N − 1) augmented examples within a minibatch as negative examples.
        """
        N = 2 * batch_size

        z = torch.cat((z_i, z_j), dim=0)

        # pairwise l2 distace
        sim = torch.cdist(z, z, p=2)

        sim_i_j = torch.diag(sim, batch_size)
        sim_j_i = torch.diag(sim, -batch_size)

        positive_samples = torch.cat((sim_i_j, sim_j_i), dim=0).reshape(N, 1)
        alignment = positive_samples.sum()

        # pairwise l2 distace
        sim = torch.cdist(origin_z, origin_z, p=2)
        mask = torch.ones((batch_size, batch_size), dtype=bool)
        mask = mask.fill_diagonal_(0)
        negative_samples = sim[mask].reshape(batch_size, -1)
        uniformity = torch.exp(-2 * negative_samples).sum()

        return alignment, uniformity
