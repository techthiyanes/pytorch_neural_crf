import numpy as np
import torch
from typing import List, Tuple, Dict
from common import Instance
import pickle
import torch.optim as optim

import torch.nn as nn
from transformers import AdamW, PreTrainedTokenizer


from config import PAD, ContextEmb, Config
from termcolor import colored

def log_sum_exp_pytorch(vec: torch.Tensor) -> torch.Tensor:
    """
    Calculate the log_sum_exp trick for the tensor.
    :param vec: [batchSize * from_label * to_label].
    :return: [batchSize * to_label]
    """
    maxScores, idx = torch.max(vec, 1)
    maxScores[maxScores == -float("Inf")] = 0
    maxScoresExpanded = maxScores.view(vec.shape[0] ,1 , vec.shape[2]).expand(vec.shape[0], vec.shape[1], vec.shape[2])
    return maxScores + torch.log(torch.sum(torch.exp(vec - maxScoresExpanded), 1))

def batching_list_instances(config: Config, insts: List[Instance]):
    train_num = len(insts)
    batch_size = config.batch_size
    total_batch = train_num // batch_size + 1 if train_num % batch_size != 0 else train_num // batch_size
    batched_data = []
    for batch_id in range(total_batch):
        one_batch_insts = insts[batch_id * batch_size:(batch_id + 1) * batch_size]
        if config.embedder_type!= "normal":
            batched_data.append(bert_batching(config, one_batch_insts))
        else:
            batched_data.append(simple_batching(config, one_batch_insts))

    return batched_data

def bert_batching(config, insts: List[Instance]) -> Dict[str,torch.Tensor]:
    batch_size = len(insts)
    batch_data = insts

    word_seq_len = torch.LongTensor(list(map(lambda inst: len(inst.input.words), batch_data)))
    max_seq_len = word_seq_len.max()

    token_seq_len = torch.LongTensor(list(map(lambda inst: len(inst.word_ids), batch_data)))
    max_tok_seq_len = token_seq_len.max()

    word_seq_tensor = torch.zeros([batch_size, max_tok_seq_len], dtype=torch.long)
    orig_to_tok_index = torch.zeros([batch_size, max_seq_len], dtype=torch.long)
    label_seq_tensor = torch.zeros([batch_size, max_seq_len], dtype=torch.long)
    """
    Bert model needs an input mask
    """
    input_mask = torch.zeros([batch_size, max_tok_seq_len], dtype=torch.long)
    for idx in range(batch_size):
        word_seq_tensor[idx, :token_seq_len[idx]] = torch.LongTensor(batch_data[idx].word_ids)
        orig_to_tok_index[idx, :word_seq_len[idx]] = torch.LongTensor(batch_data[idx].orig_to_tok_index)
        input_mask[idx, :token_seq_len[idx]]  = 1
        if batch_data[idx].output_ids:
            label_seq_tensor[idx, :word_seq_len[idx]] = torch.LongTensor(batch_data[idx].output_ids)

    return  {
        "words": word_seq_tensor.to(config.device),
        "word_seq_lens": word_seq_len.to(config.device),
        "orig_to_tok_index": orig_to_tok_index.to(config.device),
        "input_mask": input_mask.to(config.device),
        "labels": label_seq_tensor.to(config.device)
    }

def simple_batching(config, insts: List[Instance]) -> Dict[str,torch.Tensor]:

    """
    batching these instances together and return tensors. The seq_tensors for word and char contain their word id and char id.
    :return 
        word_seq_tensor: Shape: (batch_size, max_seq_length)
        word_seq_len: Shape: (batch_size), the length of each sentence in a batch.
        context_emb_tensor: Shape: (batch_size, max_seq_length, context_emb_size)
        char_seq_tensor: Shape: (batch_size, max_seq_len, max_char_seq_len)
        char_seq_len: Shape: (batch_size, max_seq_len), 
        label_seq_tensor: Shape: (batch_size, max_seq_length)
    """
    batch_size = len(insts)
    batch_data = insts
    # probably no need to sort because we will sort them in the model instead.
    # batch_data = sorted(insts, key=lambda inst: len(inst.input.words), reverse=True) ##object-based not direct copy
    word_seq_len = torch.LongTensor(list(map(lambda inst: len(inst.input.words), batch_data)))
    max_seq_len = word_seq_len.max()

    ## usually these two lengths are same, but if we use BERT tokenization, max_seq_len could be larger

    # NOTE: Use 1 here because the CharBiLSTM accepts
    char_seq_len = torch.LongTensor([list(map(len, inst.input.words)) + [1] * (int(max_seq_len) - len(inst.input.words)) for inst in batch_data])
    max_char_seq_len = char_seq_len.max()

    context_emb_tensor = None
    if config.context_emb != ContextEmb.none:
        emb_size = insts[0].elmo_vec.shape[1]
        context_emb_tensor = torch.zeros([batch_size, max_seq_len, emb_size])

    word_seq_tensor = torch.zeros([batch_size, max_seq_len], dtype=torch.long)
    label_seq_tensor =  torch.zeros([batch_size, max_seq_len], dtype=torch.long)
    char_seq_tensor = torch.zeros([batch_size, max_seq_len, max_char_seq_len], dtype=torch.long)

    for idx in range(batch_size):

        word_seq_tensor[idx, :word_seq_len[idx]] = torch.LongTensor(batch_data[idx].word_ids)
        if batch_data[idx].output_ids:
            label_seq_tensor[idx, :word_seq_len[idx]] = torch.LongTensor(batch_data[idx].output_ids)
        if config.context_emb != ContextEmb.none:
            context_emb_tensor[idx, :word_seq_len[idx], :] = torch.from_numpy(batch_data[idx].elmo_vec)

        for word_idx in range(word_seq_len[idx]):
            char_seq_tensor[idx, word_idx, :char_seq_len[idx, word_idx]] = torch.LongTensor(batch_data[idx].char_ids[word_idx])
        for wordIdx in range(word_seq_len[idx], max_seq_len):
            char_seq_tensor[idx, wordIdx, 0: 1] = torch.LongTensor([config.char2idx[PAD]])   ###because line 119 makes it 1, every single character should have a id. but actually 0 is enough

    word_seq_tensor = word_seq_tensor.to(config.device)
    label_seq_tensor = label_seq_tensor.to(config.device)
    char_seq_tensor = char_seq_tensor.to(config.device)
    word_seq_len = word_seq_len.to(config.device)
    char_seq_len = char_seq_len.to(config.device)

    return {
        "words" : word_seq_tensor,
        "word_seq_lens": word_seq_len,
        "context_emb" : context_emb_tensor,
        "chars" : char_seq_tensor,
        "char_seq_lens": char_seq_len,
        "labels" : label_seq_tensor
    }


def lr_decay(config, optimizer: optim.Optimizer, epoch: int) -> optim.Optimizer:
    """
    Method to decay the learning rate
    :param config: configuration
    :param optimizer: optimizer
    :param epoch: epoch number
    :return:
    """
    lr = config.learning_rate / (1 + config.lr_decay * (epoch - 1))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    print('learning rate is set to: ', lr)
    return optimizer


def load_elmo_vec(file: str, insts: List[Instance]):
    """
    Load the elmo vectors and the vector will be saved within each instance with a member `elmo_vec`
    :param file: the vector files for the ELMo vectors
    :param insts: list of instances
    :return:
    """
    f = open(file, 'rb')
    all_vecs = pickle.load(f)  # variables come out in the order you put them in
    f.close()
    size = 0
    for vec, inst in zip(all_vecs, insts):
        inst.elmo_vec = vec
        size = vec.shape[1]
        assert(vec.shape[0] == len(inst.input.words))
    return size



def get_optimizer(config: Config, model: nn.Module):
    params = model.parameters()
    if config.optimizer.lower() == "sgd":
        print(colored("Using SGD: lr is: {}, L2 regularization is: {}".format(config.learning_rate, config.l2), 'yellow'))
        return optim.SGD(params, lr=config.learning_rate, weight_decay=float(config.l2))
    elif config.optimizer.lower() == "adam":
        print(colored(f"Using Adam, with learning rate: {config.learning_rate}", 'yellow'))
        return optim.Adam(model.parameters(), lr=config.learning_rate)
    elif config.optimizer.lower() == "adamw":
        print(colored(f"Using AdamW optimizeer with {config.learning_rate} learning rate, "
                      f"eps: {1e-8}", 'yellow'))
        return AdamW(model.parameters(), lr=config.learning_rate, eps=1e-8)
    else:
        print("Illegal optimizer: {}".format(config.optimizer))
        exit(1)



def write_results(filename: str, insts):
    f = open(filename, 'w', encoding='utf-8')
    for inst in insts:
        for i in range(len(inst.input)):
            words = inst.input.ori_words
            output = inst.output
            prediction = inst.prediction
            assert len(output) == len(prediction)
            f.write("{}\t{}\t{}\t{}\n".format(i, words[i], output[i], prediction[i]))
        f.write("\n")
    f.close()



def tokenize_instance(transformer_tokenizer: PreTrainedTokenizer, insts: List[Instance]) -> None:
    """
    Tokenize the instances for BERT-based model
    :param tokenizer: Pretrained_Tokenizer from the transformer packages
    :param insts: List[List[Instance]
    :return: None
    """
    for inst in insts:
        tokens = [] ## store the wordpiece tokens
        orig_to_tok_index = []
        for i, word in enumerate(inst.input.ori_words):
            """
            Note: by default, we use the first wordpiece token to represent the word
            If you want to do something else (e.g., use last wordpiece to represent), modify them here.
            """
            orig_to_tok_index.append(len(tokens))
            ## tokenize the word into word_piece
            word_tokens = transformer_tokenizer.tokenize(word)
            for sub_token in word_tokens:
                tokens.append(sub_token)

        input_ids = transformer_tokenizer.convert_tokens_to_ids(['[CLS]'] + tokens + ['SEP'])
        inst.word_ids = input_ids
        inst.orig_to_tok_index = orig_to_tok_index


