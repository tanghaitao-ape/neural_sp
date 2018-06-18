#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""Define evaluation method of word-level models (CSJ corpus)."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from tqdm import tqdm
import pandas as pd

from utils.io.labels.word import Word2char
from utils.evaluation.edit_distance import compute_wer
from utils.evaluation.resolving_unk import resolve_unk
from utils.evaluation.normalization import normalize


def eval_word(models, dataset, eval_batch_size,
              beam_width, max_decode_len, min_decode_len=1,
              beam_width_sub=1, max_decode_len_sub=200, min_decode_len_sub=1,
              length_penalty=0, coverage_penalty=0,
              rnnlm_weight=0, rnnlm_weight_sub=0,
              progressbar=False, resolving_unk=False, a2c_oracle=False,
              joint_decoding=None, score_sub_weight=0):
    """Evaluate trained model by Word Error Rate.
    Args:
        models (list): the models to evaluate
        dataset: An instance of a `Dataset' class
        eval_batch_size (int): the batch size when evaluating the model
        beam_width (int): the size of beam in ths main task
        max_decode_len (int): the maximum sequence length of tokens in the main task
        min_decode_len (int): the minimum sequence length of tokens in the main task
        beam_width_sub (int): the size of beam in ths sub task
            This is used for the nested attention
        max_decode_len_sub (int): the maximum sequence length of tokens in the sub task
        min_decode_len_sub (int): the minimum sequence length of tokens in the sub task
        length_penalty (float): length penalty in the beam search decoding
        coverage_penalty (float): coverage penalty in the beam search decoding
        rnnlm_weight (float): the weight of RNNLM score of the main task in the beam search decoding
        rnnlm_weight_sub (float): the weight of RNNLM score of the sub task in the beam search decoding
        progressbar (bool): if True, visualize the progressbar
        resolving_unk (bool):
        a2c_oracle (bool):
        joint_decoding (bool):
        score_sub_weight (float):
    Returns:
        wer (float): Word error rate
        df_word (pd.DataFrame): dataframe of substitution, insertion, and deletion
    """
    # Reset data counter
    dataset.reset()

    model = models[0]
    # TODO: fix this

    if model.model_type == 'hierarchical_attention' and joint_decoding:
        word2char = Word2char(dataset.vocab_file_path,
                              dataset.vocab_file_path_sub)

    wer = 0
    sub, ins, dele, = 0, 0, 0
    num_words = 0
    if progressbar:
        pbar = tqdm(total=len(dataset))  # TODO: fix this
    while True:
        batch, is_new_epoch = dataset.next(batch_size=eval_batch_size)

        # Decode
        if model.model_type == 'nested_attention':
            if a2c_oracle:
                if dataset.is_test:
                    max_label_num = 0
                    for b in range(len(batch['xs'])):
                        if max_label_num < len(list(batch['ys_sub'][b])):
                            max_label_num = len(list(batch['ys_sub'][b]))

                    ys_sub = []
                    for b in range(len(batch['xs'])):
                        indices = dataset.char2idx(batch['ys_sub'][b])
                        ys_sub += [indices]
                        # NOTE: transcript is seperated by space('_')
                else:
                    ys_sub = batch['ys_sub']
            else:
                ys_sub = None

            best_hyps, aw, best_hyps_sub, aw_sub, _, perm_idx = model.decode(
                batch['xs'],
                beam_width=beam_width,
                max_decode_len=max_decode_len,
                min_decode_len=min_decode_len,
                beam_width_sub=beam_width_sub,
                max_decode_len_sub=max_label_num if a2c_oracle else max_decode_len_sub,
                min_decode_len_sub=min_decode_len_sub,
                length_penalty=length_penalty,
                coverage_penalty=coverage_penalty,
                rnnlm_weight=rnnlm_weight,
                rnnlm_weight_sub=rnnlm_weight_sub,
                teacher_forcing=a2c_oracle,
                ys_sub=ys_sub)
        elif model.model_type == 'hierarchical_attention' and joint_decoding:
            best_hyps, aw, best_hyps_sub, aw_sub, perm_idx = model.decode(
                batch['xs'],
                beam_width=beam_width,
                max_decode_len=max_decode_len,
                min_decode_len=min_decode_len,
                length_penalty=length_penalty,
                coverage_penalty=coverage_penalty,
                rnnlm_weight=rnnlm_weight,
                joint_decoding=joint_decoding,
                space_index=dataset.char2idx('_')[0],
                oov_index=dataset.word2idx('OOV')[0],
                word2char=word2char,
                idx2word=dataset.idx2word,
                idx2char=dataset.idx2char,
                score_sub_weight=score_sub_weight)
        else:
            best_hyps, aw, perm_idx = model.decode(
                batch['xs'],
                beam_width=beam_width,
                max_decode_len=max_decode_len,
                min_decode_len=min_decode_len,
                length_penalty=length_penalty,
                coverage_penalty=coverage_penalty,
                rnnlm_weight=rnnlm_weight)
            if resolving_unk:
                best_hyps_sub, aw_sub, _ = model.decode(
                    batch['xs'],
                    beam_width=beam_width,
                    max_decode_len=max_decode_len_sub,
                    min_decode_len=min_decode_len_sub,
                    length_penalty=length_penalty,
                    coverage_penalty=coverage_penalty,
                    rnnlm_weight_sub=rnnlm_weight_sub,
                    task_index=1)

        ys = [batch['ys'][i] for i in perm_idx]

        for b in range(len(batch['xs'])):
            # Reference
            if dataset.is_test:
                str_ref = ys[b]
                # NOTE: transcript is seperated by space('_')
            else:
                str_ref = dataset.idx2word(ys[b])

            # Hypothesis
            str_hyp = dataset.idx2word(best_hyps[b])

            # Resolving UNK
            if resolving_unk and 'OOV' in str_hyp:
                str_hyp = resolve_unk(
                    str_hyp, best_hyps_sub[b], aw[b], aw_sub[b], dataset.idx2char)
                str_hyp = str_hyp.replace('*', '')

            str_ref = normalize(str_ref, remove_tokens=['@'])
            str_hyp = normalize(str_hyp, remove_tokens=['@', '>'])
            # NOTE: @ means <sp>

            # Compute WER
            try:
                wer_b, sub_b, ins_b, del_b = compute_wer(
                    ref=str_ref.split('_'),
                    hyp=str_hyp.split('_'),
                    normalize=False)
                wer += wer_b
                sub += sub_b
                ins += ins_b
                dele += del_b
                num_words += len(str_ref.split('_'))
            except:
                pass

            if progressbar:
                pbar.update(1)

        if is_new_epoch:
            break

    if progressbar:
        pbar.close()

    # Reset data counters
    dataset.reset()

    wer /= num_words
    sub /= num_words
    ins /= num_words
    dele /= num_words

    df_word = pd.DataFrame({'SUB': [sub], 'INS': [ins], 'DEL': [dele]},
                           columns=['SUB', 'INS', 'DEL'],
                           index=['WER'])

    return wer, df_word
