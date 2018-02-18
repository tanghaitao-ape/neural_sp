#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""(Hierarchical) RNN encoders (pytorch)."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.nn.functional as F
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

from models.pytorch.linear import LinearND
from models.pytorch.encoders.cnn import CNNEncoder
from utils.io.variable import var2np, np2var


class RNNEncoder(nn.Module):
    """RNN encoder.
    Args:
        input_size (int): the dimension of input features
        rnn_type (string): lstm or gru or rnn
        bidirectional (bool): if True, use the bidirectional encoder
        num_units (int): the number of units in each layer
        num_proj (int): the number of nodes in the projection layer
        num_layers (int): the number of layers
        dropout_input (float): the probability to drop nodes in input-hidden connection
        dropout_hidden (float): the probability to drop nodes in hidden-hidden connection
        subsample_list (list): subsample in the corresponding layers (True)
            ex.) [False, True, True, False] means that downsample is conducted
                in the 2nd and 3rd layers.
        subsample_type (string, optional): drop or concat
        use_cuda (bool, optional): if True, use GPUs
        batch_first (bool, optional): if True, batch-major computation will be
            performed
        merge_bidirectional (bool, optional): if True, sum bidirectional outputs
        pack_sequence (bool, optional):
        num_stack (int, optional): the number of frames to stack
        splice (int, optional): frames to splice. Default is 1 frame.
        conv_channels (list, optional): the number of channles in CNN layers
        conv_kernel_sizes (list, optional): the size of kernels in CNN layers
        conv_strides (list, optional): strides in CNN layers
        poolings (list, optional): the size of poolings in CNN layers
        activation (string, optional): The activation function of CNN layers.
            Choose from relu or prelu or hard_tanh or maxout
        batch_norm (bool, optional):
        residual (bool, optional):
        dense_residual (bool, optional):
        num_layers_sub (int): the number of layers in the sub task
    """

    def __init__(self,
                 input_size,
                 rnn_type,
                 bidirectional,
                 num_units,
                 num_proj,
                 num_layers,
                 dropout_input,
                 dropout_hidden,
                 subsample_list=[],
                 subsample_type='drop',
                 use_cuda=False,
                 batch_first=False,
                 merge_bidirectional=False,
                 pack_sequence=True,
                 num_stack=1,
                 splice=1,
                 conv_channels=[],
                 conv_kernel_sizes=[],
                 conv_strides=[],
                 poolings=[],
                 activation='relu',
                 batch_norm=False,
                 residual=False,
                 dense_residual=False,
                 num_layers_sub=0):

        super(RNNEncoder, self).__init__()

        if len(subsample_list) > 0 and len(subsample_list) != num_layers:
            raise ValueError(
                'subsample_list must be the same size as num_layers.')
        if subsample_type not in ['drop', 'concat']:
            raise TypeError('subsample_type must be "drop" or "concat".')
        if num_layers_sub < 0 or (num_layers_sub > 1 and num_layers < num_layers_sub):
            raise ValueError(
                'Set num_layers_sub between 1 to num_layers.')

        self.rnn_type = rnn_type
        self.bidirectional = bidirectional
        self.num_directions = 2 if bidirectional else 1
        self.num_units = num_units
        self.num_proj = num_proj if num_proj is not None else 0
        self.num_layers = num_layers
        self.use_cuda = use_cuda
        self.batch_first = batch_first
        self.merge_bidirectional = merge_bidirectional
        self.pack_sequence = pack_sequence

        # Setting for hierarchical encoder
        self.num_layers_sub = num_layers_sub

        # Setting for subsampling
        if len(subsample_list) == 0:
            self.subsample_list = [False] * num_layers
        else:
            self.subsample_list = subsample_list
        self.subsample_type = subsample_type
        # This implementation is bases on
        # https://arxiv.org/abs/1508.01211
        #     Chan, William, et al. "Listen, attend and spell."
        #         arXiv preprint arXiv:1508.01211 (2015).

        # Setting for residual connection
        assert not (residual and dense_residual)
        self.residual = residual
        self.dense_residual = dense_residual
        subsample_last_layer = 0
        for i_layer_reverse, is_subsample in enumerate(subsample_list[::-1]):
            if is_subsample:
                subsample_last_layer = num_layers - i_layer_reverse
                break
        self.residual_start_layer = subsample_last_layer + 1
        # NOTE: このレイヤの出力からres_outputs_listに入れていく

        # Dropout for input-hidden connection
        self.dropout_input = nn.Dropout(p=dropout_input)

        # Setting for CNNs before RNNs
        if len(conv_channels) > 0 and len(conv_channels) == len(conv_kernel_sizes) and len(conv_kernel_sizes) == len(conv_strides):
            assert num_stack == 1 and splice == 1
            self.conv = CNNEncoder(input_size,
                                   conv_channels=conv_channels,
                                   conv_kernel_sizes=conv_kernel_sizes,
                                   conv_strides=conv_strides,
                                   poolings=poolings,
                                   dropout_input=0,
                                   dropout_hidden=dropout_hidden,
                                   activation=activation,
                                   batch_norm=batch_norm)
            input_size = self.conv.output_size
        else:
            input_size = input_size * splice * num_stack
            self.conv = None

        # Fast implementation without using torch.nn.utils.rnn.PackedSequence
        if sum(self.subsample_list) == 0 and self.num_proj == 0 and not residual and not dense_residual and num_layers_sub == 0:
            self.fast_impl = True

            if rnn_type == 'lstm':
                rnn = nn.LSTM(input_size,
                              hidden_size=num_units,
                              num_layers=num_layers,
                              bias=True,
                              batch_first=batch_first,
                              dropout=dropout_hidden,
                              bidirectional=bidirectional)
            elif rnn_type == 'gru':
                rnn = nn.GRU(input_size,
                             hidden_size=num_units,
                             num_layers=num_layers,
                             bias=True,
                             batch_first=batch_first,
                             dropout=dropout_hidden,
                             bidirectional=bidirectional)
            elif rnn_type == 'rnn':
                rnn = nn.RNN(input_size,
                             hidden_size=num_units,
                             num_layers=num_layers,
                             bias=True,
                             batch_first=batch_first,
                             dropout=dropout_hidden,
                             bidirectional=bidirectional)

            setattr(self, rnn_type, rnn)
            # NOTE: pytorch introduces a dropout layer on the outputs of
            # each RNN layer EXCEPT the last layer

            # Dropout for hidden-output connection
            self.dropout_last = nn.Dropout(p=dropout_hidden)

        else:
            self.fast_impl = False

            for i_layer in range(num_layers):
                if i_layer == 0:
                    encoder_input_size = input_size
                elif self.num_proj > 0:
                    encoder_input_size = num_proj
                    if subsample_type == 'concat' and i_layer > 0 and self.subsample_list[i_layer - 1]:
                        encoder_input_size *= 2
                else:
                    encoder_input_size = num_units * self.num_directions
                    if subsample_type == 'concat' and i_layer > 0 and self.subsample_list[i_layer - 1]:
                        encoder_input_size *= 2

                if rnn_type == 'lstm':
                    rnn_i = nn.LSTM(encoder_input_size,
                                    hidden_size=num_units,
                                    num_layers=1,
                                    bias=True,
                                    batch_first=batch_first,
                                    dropout=0,
                                    bidirectional=bidirectional)

                elif rnn_type == 'gru':
                    rnn_i = nn.GRU(encoder_input_size,
                                   hidden_size=num_units,
                                   num_layers=1,
                                   bias=True,
                                   batch_first=batch_first,
                                   dropout=0,
                                   bidirectional=bidirectional)
                elif rnn_type == 'rnn':
                    rnn_i = nn.RNN(encoder_input_size,
                                   hidden_size=num_units,
                                   num_layers=1,
                                   bias=True,
                                   batch_first=batch_first,
                                   dropout=0,
                                   bidirectional=bidirectional)
                else:
                    raise ValueError(
                        'rnn_type must be "lstm" or "gru" or "rnn".')

                if self.subsample_list[i_layer]:
                    setattr(self, 'p' + rnn_type + '_l' + str(i_layer), rnn_i)
                else:
                    setattr(self, rnn_type + '_l' + str(i_layer), rnn_i)

                # Dropout for hidden-hidden or hidden-output connection
                setattr(self, 'dropout_l' + str(i_layer),
                        nn.Dropout(p=dropout_hidden))

                if i_layer != self.num_layers - 1 and self.num_proj > 0:
                    proj_i = LinearND(num_units * self.num_directions, num_proj,
                                      dropout=dropout_hidden)
                    setattr(self, 'proj_l' + str(i_layer), proj_i)

    def forward(self, xs, x_lens, volatile=False):
        """Forward computation.
        Args:
            xs (torch.autograd.Variable, float): A tensor of size
                `[B, T, input_size]`
            x_lens (torch.autograd.Variable, int): A tensor of size `[B]`
            volatile (bool, optional): if True, the history will not be saved.
                This should be used in inference model for memory efficiency.
        Returns:
            xs (torch.autograd.Variable, float):
                if batch_first is True, a tensor of size
                    `[B, T // sum(subsample_list), num_units (* num_directions)]`
                else
                    `[T // sum(subsample_list), B, num_units (* num_directions)]`
            x_lens (torch.autograd.Variable, int): A tensor of size `[B]`
            OPTION:
                xs_sub (torch.autograd.Variable, float):
                    if batch_first is True, a tensor of size
                        `[B, T // sum(subsample_list), num_units (* num_directions)]`
                    else
                        `[T // sum(subsample_list), B, num_units (* num_directions)]`
                x_lens_sub (torch.autograd.Variable, int): A tensor of size `[B]`
            perm_idx (torch.autograd.Variable, int): A tensor of size `[B]`
        """
        batch_size = xs.size(0)
        use_cuda = xs.is_cuda

        # Dropout for inputs-hidden connection
        xs = self.dropout_input(xs)

        # Path through CNN layers before RNN layers
        if self.conv is not None:
            xs, x_lens = self.conv(xs, x_lens)

        # Sort xs by lengths in descending order
        if self.pack_sequence:
            x_lens, perm_idx = x_lens.sort(dim=0, descending=True)
            xs = xs[perm_idx]
            # NOTE: batch-first yet here
            # NOTE: must be descending order for pack_padded_sequence
        else:
            perm_idx = None
        x_lens = var2np(x_lens).tolist()

        if not self.batch_first:
            # Convert to the time-major
            xs = xs.transpose(0, 1).contiguous()

        if self.fast_impl:
            # Initialize hidden states (and memory cells) per mini-batch
            h_0 = _init_hidden(batch_size=batch_size,
                               rnn_type=self.rnn_type,
                               num_units=self.num_units,
                               num_directions=self.num_directions,
                               num_layers=self.num_layers,
                               use_cuda=use_cuda,
                               volatile=volatile)

            # Pack encoder inputs
            if self.pack_sequence:
                if not isinstance(xs, torch.nn.utils.rnn.PackedSequence):
                    xs = pack_padded_sequence(
                        xs, x_lens, batch_first=self.batch_first)

            # Path through RNN
            xs, _ = getattr(self, self.rnn_type)(xs, hx=h_0)

            # Unpack encoder outputs
            if self.pack_sequence:
                xs, unpacked_seq_len = pad_packed_sequence(
                    xs, batch_first=self.batch_first, padding_value=0)
                # assert x_lens == unpacked_seq_len

            # Dropout for hidden-output connection
            xs = self.dropout_last(xs)

        else:
            # Initialize hidden states (and memory cells) per mini-batch
            h_0 = _init_hidden(batch_size=batch_size,
                               rnn_type=self.rnn_type,
                               num_units=self.num_units,
                               num_directions=self.num_directions,
                               num_layers=1,
                               use_cuda=use_cuda,
                               volatile=volatile)

            res_outputs_list = []
            # NOTE: exclude residual connection from the raw inputs
            for i_layer in range(self.num_layers):

                torch.cuda.empty_cache()

                # Pack i_layer-th encoder xs
                if self.pack_sequence:
                    if not isinstance(xs, torch.nn.utils.rnn.PackedSequence):
                        xs = pack_padded_sequence(
                            xs, x_lens, batch_first=self.batch_first)

                # Path through RNN
                if self.subsample_list[i_layer]:
                    xs, _ = getattr(self, 'p' + self.rnn_type +
                                    '_l' + str(i_layer))(xs, hx=h_0)
                else:
                    xs, _ = getattr(self, self.rnn_type + '_l' +
                                    str(i_layer))(xs, hx=h_0)

                # Unpack i_layer-th encoder outputs
                if self.pack_sequence:
                    xs, unpacked_seq_len = pad_packed_sequence(
                        xs, batch_first=self.batch_first, padding_value=0)
                    # assert x_lens == unpacked_seq_len

                # Dropout for hidden-hidden or hidden-output connection
                xs = getattr(self, 'dropout_l' + str(i_layer))(xs)

                # Pick up outputs in the sub task before the projection layer
                if self.num_layers_sub >= 1 and i_layer == self.num_layers_sub - 1:
                    xs_sub = xs

                    # Wrap by Variable again
                    x_lens_sub = np2var(
                        x_lens, dtype='int', use_cuda=use_cuda, backend='pytorch')

                # NOTE: Exclude the last layer
                if i_layer != self.num_layers - 1:
                    if self.residual or self.dense_residual or self.num_proj > 0 or self.subsample_list[i_layer]:

                        # Projection layer (affine transformation)
                        if self.num_proj > 0:
                            xs = F.tanh(
                                getattr(self, 'proj_l' + str(i_layer))(xs))

                        # Subsampling
                        if self.subsample_list[i_layer]:
                            # Pick up features at odd time step
                            if self.subsample_type == 'drop':
                                if self.batch_first:
                                    xs = xs[:, ::2, :]
                                else:
                                    xs = xs[::2, :, :]

                            # Concatenate the successive frames
                            elif self.subsample_type == 'concat':
                                if self.batch_first:
                                    xs = [torch.cat([xs[:, t - 1:t, :], xs[:, t:t + 1, :]], dim=2)
                                          for t in range(xs.size(1)) if (t + 1) % 2 == 0]
                                    xs = torch.cat(xs, dim=1)
                                else:
                                    xs = [torch.cat([xs[t - 1:t, :, :], xs[t:t + 1, :, :]], dim=2)
                                          for t in range(xs.size(0)) if (t + 1) % 2 == 0]
                                    xs = torch.cat(xs, dim=0)

                            # Update x_lens
                            if self.batch_first:
                                x_lens = [x.size(0) for x in xs]
                            else:
                                x_lens = [xs[:, i].size(0)
                                          for i in range(xs.size(1))]

                        # Residual connection
                        elif self.residual or self.dense_residual:
                            if i_layer >= self.residual_start_layer - 1:
                                for xs_lower in res_outputs_list:
                                    xs = xs + xs_lower
                                if self.residual:
                                    res_outputs_list = [xs]
                                elif self.dense_residual:
                                    res_outputs_list.append(xs)

        # Wrap by Variable again
        x_lens = np2var(
            x_lens, dtype='int', use_cuda=xs.is_cuda, backend='pytorch')

        # Sum bidirectional outputs
        if self.bidirectional and self.merge_bidirectional:
            xs = xs[:, :, :self.num_units] + xs[:, :, self.num_units:]

        del h_0

        # sub task (optional)
        if self.num_layers_sub >= 1:

            # Sum bidirectional outputs
            if self.bidirectional and self.merge_bidirectional:
                xs_sub = xs_sub[:, :, :self.num_units] + \
                    xs_sub[:, :, self.num_units:]
            return xs, x_lens, xs_sub, x_lens_sub, perm_idx
        else:
            return xs, x_lens, perm_idx


def _init_hidden(batch_size, rnn_type, num_units, num_directions,
                 num_layers, use_cuda, volatile):
    """Initialize hidden states.
    Args:
        batch_size (int): the size of mini-batch
        rnn_type (string): lstm or gru or rnn
        num_units (int):
        num_directions (int):
        num_layers (int):
        use_cuda (bool, optional):
        volatile (bool): if True, the history will not be saved.
            This should be used in inference model for memory efficiency.
    Returns:
        if rnn_type is 'lstm', return a tuple of tensors (h_0, c_0).
            h_0 (torch.autograd.Variable, float): A tensor of size
                `[num_layers * num_directions, batch_size, num_units]`
            c_0 (torch.autograd.Variable, float): A tensor of size
                `[num_layers * num_directions, batch_size, num_units]`
        otherwise return h_0.
    """
    h_0 = Variable(torch.zeros(
        num_layers * num_directions, batch_size, num_units))
    if volatile:
        h_0.volatile = True
    if use_cuda:
        h_0 = h_0.cuda()

    if rnn_type == 'lstm':
        c_0 = Variable(torch.zeros(
            num_layers * num_directions, batch_size, num_units))
        if volatile:
            c_0.volatile = True
        if use_cuda:
            c_0 = c_0.cuda()

        return (h_0, c_0)
    else:
        return h_0
