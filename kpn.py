import math
import torch
import torch.nn as nn
import torch.nn.init as init
import torch.nn.functional as F

class TransformerBlock(nn.Module):
    def __init__(self, input_size, is_layer_norm=False):
        super(TransformerBlock, self).__init__()
        self.is_layer_norm = is_layer_norm
        if is_layer_norm:
            self.layer_morm = nn.LayerNorm(normalized_shape=input_size)

        self.relu = nn.ReLU()
        self.linear1 = nn.Linear(input_size, input_size)
        self.linear2 = nn.Linear(input_size, input_size)
        self.init_weights()

    def init_weights(self):
        init.xavier_normal_(self.linear1.weight)
        init.xavier_normal_(self.linear2.weight)

    def FFN(self, X):
        return self.linear2(self.relu(self.linear1(X)))

    def forward(self, Q, K, V, episilon=1e-8):
        '''
        :param Q: (batch_size, max_r_words, embedding_dim)
        :param K: (batch_size, max_u_words, embedding_dim)
        :param V: (batch_size, max_u_words, embedding_dim)
        :return: output: (batch_size, max_r_words, embedding_dim)  same size as Q
        '''
        dk = torch.Tensor([max(1.0, Q.size(-1))]).cuda()
        Q_K = Q.bmm(K.permute(0, 2, 1)) / (torch.sqrt(dk) + episilon)
        # (batch_size, max_r_words, max_u_words)
        Q_K_score = F.softmax(Q_K, dim=-1)
        V_att = Q_K_score.bmm(V)
        if self.is_layer_norm:
            # (batch_size, max_r_words, embedding_dim)
            X = self.layer_morm(Q + V_att)
            output = self.layer_morm(self.FFN(X) + X)
        else:
            X = Q + V_att
            output = self.FFN(X) + X
        return output


def dot_attention(q, k, v, v_mask=None, dropout=None):
    attention_weights = torch.matmul(q, k.transpose(-1, -2))
    if v_mask is not None:
        attention_weights *= v_mask.unsqueeze(1)
    attention_weights = F.softmax(attention_weights, -1)
    if dropout is not None:
        attention_weights = dropout(attention_weights)
    output = torch.matmul(attention_weights, v)
    return output


class Attention(nn.Module):
    def __init__(self, input_size, hidden_size):
        super(Attention, self).__init__()
        self.linear1 = nn.Linear(in_features=input_size, out_features=hidden_size)
        self.linear2 = nn.Linear(in_features=hidden_size, out_features=1)
        self.init_weights()

    def init_weights(self):
        init.xavier_normal_(self.linear1.weight)
        init.xavier_normal_(self.linear2.weight)

    def forward(self, X, mask=None):
        '''
        :param X:
        :param mask:   http://juditacs.github.io/2018/12/27/masked-attention.html
        :return:
        '''
        M = F.tanh(self.linear1(X))  # (batch_size, max_u_words, embedding_dim)
        M = self.linear2(M)
        if mask is not None:
            M[~mask] = float('-inf')
        score = F.softmax(M, dim=1)    # (batch_size, max_u_words, 1)

        output = (score * X).sum(dim=1)  # (batch_size, embedding_dim)
        return output



class KPN(nn.Module):
    def __init__(self, dataset=None, embedding=None, device=None):
        super(KPN, self).__init__()
        self.max_context_len = 30
        self.max_response_len = 30
        self.n_layer = 4
        if dataset == "duconv":
            self.max_goal_len = 3
            self.max_knowledge_num = 23
            self.max_knowledge_len = 10
        else:
            self.max_goal_len = 6
            self.max_knowledge_num = 35
            self.max_knowledge_len = 30

        self.emb_size = 300
        self.hidden_size = 300
        self.embedding = nn.Embedding.from_pretrained(embedding, freeze=False, padding_idx=0)
        self.layer_norm = nn.LayerNorm(self.emb_size)
        self.vocab_size = 44234

        self.transformer_r = []
        for i in range(self.n_layer):
            self.transformer_r.append(TransformerBlock(input_size=self.emb_size))
        self.transformer_r = nn.ModuleList(self.transformer_r)

        self.transformer_rp = []
        for i in range(self.n_layer+1):
            self.transformer_rp.append(TransformerBlock(input_size=self.emb_size))
        self.transformer_rp = nn.ModuleList(self.transformer_rp)

        self.transformer_utt = TransformerBlock(input_size=self.emb_size)
        self.transformer_res = TransformerBlock(input_size=self.emb_size)
        self.transformer_ur = TransformerBlock(input_size=self.emb_size)
        self.transformer_ru = TransformerBlock(input_size=self.emb_size)



        self.selector_transformer = TransformerBlock(input_size=self.hidden_size)
        self.W_word = nn.Parameter(data=torch.Tensor(self.hidden_size, self.hidden_size, 10))
        self.v = nn.Parameter(data=torch.Tensor(10, 1))
        self.linear_word = nn.Linear(13, 1)
        self.linear_score = nn.Linear(4, 1)

        self.cos = nn.CosineSimilarity(dim=-1)

        self.A1 = nn.Parameter(data=torch.Tensor(self.hidden_size, self.hidden_size))
        self.A2 = nn.Parameter(data=torch.Tensor(self.hidden_size, self.hidden_size))

        self.affine1 = nn.Linear(in_features=6 * 6 * 64, out_features=self.hidden_size)
        if dataset == "duconv":
            self.affine2 = nn.Linear(in_features=6 * 1 * 64, out_features=self.hidden_size)
        else:
            self.affine2 = nn.Linear(in_features=6 * 6 * 64, out_features=self.hidden_size)
        self.affine_out1 = nn.Linear(self.hidden_size, 1)
        self.affine_out2 = nn.Linear(self.hidden_size, 1)
        self.affine_out3 = nn.Linear(self.hidden_size * 2, 1)
        self.affine_out4 = nn.Linear(self.hidden_size * 2, 1)

        self.affine_attn = nn.Linear(self.hidden_size, 1)

        self.cnn_2d_1 = nn.Conv2d(in_channels=4, out_channels=32, kernel_size=(3, 3))
        self.maxpooling1 = nn.MaxPool2d(kernel_size=(2, 2), stride=(2, 2))
        self.cnn_2d_2 = nn.Conv2d(in_channels=32, out_channels=64, kernel_size=(3, 3))
        self.maxpooling2 = nn.MaxPool2d(kernel_size=(2, 2), stride=(2, 2))
        self.cnn_2d_3 = nn.Conv2d(in_channels=32, out_channels=64, kernel_size=(3,3))
        self.maxpooling3 = nn.MaxPool2d(kernel_size=(3, 3), stride=(3, 3))
        self.affine22 = nn.Linear(in_features=3*3*64, out_features=self.emb_size)
        self.affine33 = nn.Linear(in_features=1*1*64, out_features = self.emb_size)
        self.attention = Attention(input_size=self.emb_size, hidden_size=self.hidden_size)

        self.affine_outtt = nn.Linear(in_features=self.hidden_size + self.emb_size, out_features=1)


        self.cnn_2d_4 = nn.Conv2d(in_channels=(self.n_layer+1)*2, out_channels=16, kernel_size=(3,3))

        self.cnn_2d_5 = nn.Conv2d(in_channels=16, out_channels=32, kernel_size=(3,3))

        self.cnn_2d_6 = nn.Conv2d(in_channels=32, out_channels=64, kernel_size=(3,3))


        self.gru_sentence = nn.LSTM(self.emb_size, self.hidden_size, batch_first=True)
        self.gru_acc1 = nn.LSTM(self.hidden_size, self.hidden_size, batch_first=True)
        self.gru_acc2 = nn.LSTM(self.hidden_size, self.hidden_size, batch_first=True)
        self.tanh = nn.Tanh()
        self.relu = nn.ReLU()
        self.softmax = nn.Softmax(dim=-1)
        self.dropout = nn.Dropout(0.1)
        self.init_weights()
        self.device = device

    def init_weights(self):
        init.uniform_(self.W_word)
        init.uniform_(self.v)
        init.uniform_(self.linear_word.weight)
        init.uniform_(self.linear_score.weight)
        init.xavier_normal_(self.A1)
        init.xavier_normal_(self.A2)
        init.xavier_normal_(self.cnn_2d_1.weight)
        init.xavier_normal_(self.cnn_2d_2.weight)
        init.xavier_normal_(self.affine1.weight)
        init.xavier_normal_(self.affine2.weight)
        init.xavier_normal_(self.affine_out1.weight)
        init.xavier_normal_(self.affine_out2.weight)
        init.xavier_normal_(self.affine_out3.weight)
        init.xavier_normal_(self.affine_out4.weight)

        init.xavier_normal_(self.cnn_2d_2.weight)
        init.xavier_normal_(self.cnn_2d_3.weight)
        init.xavier_normal_(self.affine2.weight)
        init.xavier_normal_(self.affine33.weight)
        init.xavier_normal_(self.affine22.weight)

        init.xavier_normal_(self.affine_attn.weight)
        init.xavier_normal_(self.cnn_2d_4.weight)
        init.xavier_normal_(self.affine_outtt.weight)

        init.xavier_normal_(self.cnn_2d_5.weight)

        init.xavier_normal_(self.cnn_2d_6.weight)

        for weights in [self.gru_acc1.weight_hh_l0, self.gru_acc1.weight_ih_l0]:
            init.orthogonal_(weights)
        for weights in [self.gru_acc2.weight_hh_l0, self.gru_acc2.weight_ih_l0]:
            init.orthogonal_(weights)
        for weights in [self.gru_sentence.weight_hh_l0, self.gru_sentence.weight_ih_l0]:
            init.orthogonal_(weights)

    def distance(self, A, B, C, epsilon=1e-6):
        M1 = torch.einsum("bud,dd,brd->bur", [A, B, C])
        A_norm = A.norm(dim=-1)
        C_norm = C.norm(dim=-1)
        M2 = torch.einsum("bud,brd->bur", [A, C]) / (torch.einsum("bu,br->bur", A_norm, C_norm) + epsilon)
        return M1, M2
    def uber_goal_detector(self, u_emb, g_emb): 
        kuleba = u_emb[:, -4:-1, :,:]
        su1, su2, su3, su4 = kuleba.size()
        sg1, sg2, sg3 = g_emb.size()
        kuleba = kuleba.reshape(su1, su2 * su3, su4)  # [batch_size, context_num * seq_len, emb_size]
        kuleba = kuleba.unsqueeze(2).repeat(1, 1, sg2, 1)
        g_emb_ = g_emb.unsqueeze(1).repeat(1, su2 * su3, 1, 1)
        A = self.cos(kuleba, g_emb_)  # [batch_szie, context_num * seq_len, goal_len]
        C = self.relu(A.max(dim=1)[0])  # b x g
        G = 1 - C
        g_emb = g_emb * G.unsqueeze(-1)  # g_detector: [batch, len, 1]
        return g_emb

    def goal_detector(self, u_emb, g_emb):
        """ Detect which goal has already been covered
        Arguments:
            u_emb [batch_size, context_num, seq_len, emb_size]
            g_emb [batch_size, goal_len, emb_size]
        """
        su1, su2, su3, su4 = u_emb.size()
        sg1, sg2, sg3 = g_emb.size()
        u_emb = u_emb.reshape(su1, su2 * su3, su4)  # [batch_size, context_num * seq_len, emb_size]
        u_emb_ = u_emb.unsqueeze(2).repeat(1, 1, sg2, 1)
        g_emb_ = g_emb.unsqueeze(1).repeat(1, su2 * su3, 1, 1)
        A = self.cos(u_emb_, g_emb_)  # [batch_szie, context_num * seq_len, goal_len]
        C = self.relu(A.max(dim=1)[0])  # b x g
        G = 1 - C
        g_emb = g_emb * G.unsqueeze(-1)  # g_detector: [batch, len, 1]
        return g_emb

##### TO TEST THIS ######
    def personalized_style_matching(self, bU_embedding, bR_embedding):
        pre_res_stack = []
        res_stack = []
        pre_p_stack = []
        q_stack = []

        rp_stack = []
        rq_stack = []

        pre_res_emb = bU_embedding[:,1::2,:,:].contiguous()
        pre_pos_emb = bU_embedding[:,0:-1:2,:,:].contiguous()
        query_emb = bU_embedding[:,-1,:,:]
        sr1, sr2, sr3 = bR_embedding.size()

        sp1, sp2, sp3, sp4 = pre_res_emb.size()
        pre_res_emb = pre_res_emb.view(-1, sp3, sp4)
        pre_pos_emb = pre_pos_emb.view(-1, sp3, sp4)

        res_stack.append(bR_embedding.unsqueeze(dim=1).repeat(1, sp2, 1, 1).view(-1, sr2, sr3))
        q_stack.append(query_emb.unsqueeze(dim=1).repeat(1, sp2, 1, 1).view(-1, sr2, sr3))
        
        pre_res_stack.append(pre_res_emb)
        pre_p_stack.append(pre_pos_emb)


        for i, att in enumerate(self.transformer_r):
            pre_res_emb = att(pre_res_emb, pre_res_emb, pre_res_emb)
            pre_res_stack.append(pre_res_emb)

            pre_pos_emb = att(pre_pos_emb, pre_pos_emb, pre_pos_emb)
            pre_p_stack.append(pre_pos_emb)

            bR_embedding = att(bR_embedding, bR_embedding, bR_embedding)
            res_stack.append(bR_embedding.unsqueeze(dim=1).repeat(1, sp2, 1, 1).view(-1, sr2, sr3))

            query_emb = att(query_emb, query_emb, query_emb)
            q_stack.append(query_emb.unsqueeze(dim=1).repeat(1, sp2, 1, 1).view(-1, sr2, sr3))


        for i in range(self.n_layer+1):
            rp_stack.append(self.transformer_rp[i](pre_res_stack[i], pre_p_stack[i], pre_p_stack[i]))
            rq_stack.append(self.transformer_rp[i](res_stack[i], q_stack[i], q_stack[i]))

        res_stack.extend(rq_stack)
        pre_res_stack.extend(rp_stack)

        res_stack = torch.stack(res_stack, dim=1) # 7 x bs*14 x maxlen x emb
        pre_res_stack = torch.stack(pre_res_stack, dim=1) # 7 x bs*14 x maxlen x emb

        M = torch.einsum(
            'bcid,bcjd->bcij',(res_stack, pre_res_stack)) / torch.sqrt(torch.tensor(200.0)) # bs*14 x 7 x maxlen x maxlen
        
        Z = self.relu(self.cnn_2d_4(M))
        Z = self.maxpooling1(Z)

        Z = self.relu(self.cnn_2d_5(Z))
        Z =self.maxpooling2(Z)

        Z = self.relu(self.cnn_2d_6(Z))
        Z =self.maxpooling3(Z)

        Z = Z.view(Z.size(0), -1)  # (bsz*max_utterances, *)

        V = self.tanh(self.affine33(Z))   # (bsz*max_utterances, 200)
        return V

    def utterance_selector(self, key, context):
        '''
        :param key:  (bsz, max_u_words, d)
        :param context:  (bsz, num_utterances, max_u_words, d)
        :return: score:
        '''
        key = key.mean(dim=1)
        context = context.mean(dim=2)
        s2 = torch.einsum("bud,bd->bu", context, key) / (1e-6 + torch.norm(context, dim=-1) * torch.norm(key, dim=-1, keepdim=True))
        return s2

    def knowledge_selector(self, u_emb, g_emb, k_emb):
        """ Select related knowlegde
        Arguments:
            u_emb [batch_size, context_num, seq_len, emb_size]
            g_emb [batch_size, goal_len, emb_size]
            k_emb [batch_size, knowledge_num, knowledge_len, emb_size]
        """
        g_k_score = self.utterance_selector(g_emb, k_emb)
        multi_match_score = [g_k_score]
        for i in range(1, 4):
            utterance = u_emb[:, -i, :, :]
            s2 = self.utterance_selector(utterance, k_emb)
            multi_match_score.append(s2)
        multi_match_score = torch.stack(multi_match_score, dim=-1)  # (batch_size, num_personas, max_utterances)
        match_score = self.linear_score(multi_match_score).squeeze(dim=-1).sigmoid()
        k_emb = k_emb * match_score.unsqueeze(dim=-1).unsqueeze(dim=-1)
        return k_emb, match_score

    def UR_Matching(self, bU_embedding, bR_embedding, bU_rnn, bR_rnn, type_m):
        M1, M2 = self.distance(bU_embedding, self.A1, bR_embedding)
        M3, M4 = self.distance(bU_rnn, self.A2, bR_rnn)
        M = torch.stack([M1, M2, M3, M4], dim=1)
        Z = self.relu(self.cnn_2d_1(M))
        Z = self.maxpooling1(Z)
        Z = self.relu(self.cnn_2d_2(Z))
        Z = self.maxpooling2(Z)
        Z = Z.view(Z.size(0), -1)  # (bsz*max_utterances, *)
        if type_m == 1:
            V = self.tanh(self.affine1(Z))   # (bsz*max_utterances, 300)
        else:
            V = self.tanh(self.affine2(Z))   # (bsz*max_utterances, 300)
        return V





    def forward(self, context, response, knowledge, goal, ground_truth_kg=None):
        """
        Arguments:
            context [batch_size, context_num, seq_len]
            response [batch_size, seq_len]
            knowledge [batch_size, knowledge_num, knowledge_len]
            goal [batch_size, goal_len]
        """
  


        u_emb = self.dropout(self.layer_norm(self.embedding(context)))
        r_emb = self.dropout(self.layer_norm(self.embedding(response)))
        k_emb = self.dropout(self.layer_norm(self.embedding(knowledge)))
        g_emb = self.dropout(self.layer_norm(self.embedding(goal)))

        

        su1, su2, su3, su4 = u_emb.size()  # (batch_size, context_num, seq_len, emb_size)
        sr1, sr2, sr3 = r_emb.size()  # (batch_size, seq_len, emb_size)
        sk1, sk2, sk3, sk4 = k_emb.size()

        g_emb = self.goal_detector(u_emb, g_emb)
        uber_g_emb = self.uber_goal_detector(u_emb, g_emb)
        k_emb, ks = self.knowledge_selector(u_emb, g_emb, k_emb)

        u_rnn = u_emb.view(su1 * su2, su3, su4)
        k_rnn = k_emb.view(sk1 * sk2, sk3, sk4)
        u_rnn, _ = self.gru_sentence(u_rnn)
        r_rnn, _ = self.gru_sentence(r_emb)
        k_rnn, _ = self.gru_sentence(k_rnn)
        g_rnn, _ = self.gru_sentence(g_emb)
        uber_g_rnn, _ = self.gru_sentence(uber_g_emb)
        u_rnn = u_rnn.view(su1, su2, su3, -1)
        k_rnn = k_rnn.view(sk1, sk2, sk3, -1)
       

       

        bR = r_emb.unsqueeze(dim=1).repeat(1, su2, 1, 1)
        bR_rnn = r_rnn.unsqueeze(dim=1).repeat(1, su2, 1, 1)
        bU = u_emb.view(-1, su3, su4)
        bU_rnn = u_emb.view(-1, su3, self.hidden_size)
        bR = bR.view(-1, sr2, sr3)
        bR_rnn = bR.view(-1, sr2, self.hidden_size)
        V1 = self.UR_Matching(bU, bR, bU_rnn, bR_rnn, 1)
        V1 = V1.view(su1, su2, -1)
        H1, _ = self.gru_acc1(V1)
        L1 = self.dropout(H1[:, -1, :])  # bsz, hidden
        CC1 = self.affine_out1(L1).squeeze(-1)


        g_s = self.personalized_style_matching(u_emb, r_emb)
        g_s = g_s.view(su1, su2 // 2, -1)
        LL1 = self.dropout(self.attention(g_s).squeeze(-1))
        output1 = self.affine_out1(LL1).squeeze(-1)

        bR = r_emb.unsqueeze(dim=1).repeat(1, sk2, 1, 1)
        bR_rnn = r_rnn.unsqueeze(dim=1).repeat(1, sk2, 1, 1)
        bK = k_emb.view(-1, sk3, sk4)
        bK_rnn = k_rnn.view(-1, sk3, self.hidden_size)
        bR = bR.view(-1, sr2, sr3)
        bR_rnn = bR.view(-1, sr2, self.hidden_size)
        V2 = self.UR_Matching(bK, bR, bK_rnn, bR_rnn, 2)
        V2 = V2.view(sk1, sk2, -1)
        weight = self.softmax(self.relu(self.affine_attn(V2).squeeze(-1)))  # bsz, kn_num
        L2 = self.dropout(torch.einsum("bk,bkd->bd", weight, V2))
        output2 = self.affine_out2(L2).squeeze(-1)

        g_final = g_rnn[:, -1, :]
        r_final = r_rnn[:, -1, :]
        output3 = self.affine_out3(torch.cat([g_final, r_final], dim=-1)).squeeze(-1)

        uber_g_final = uber_g_rnn[:, -1, :]
        gg4 = self.affine_out4(torch.cat([uber_g_final, r_final], dim=-1)).squeeze(-1)
        output3 = (output3 + gg4)/2
        

        logits = (output1 + output2 + output3)/3
        return logits, ks
        


