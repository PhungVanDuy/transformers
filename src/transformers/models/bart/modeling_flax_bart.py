# coding=utf-8
# Copyright 2021 The Fairseq Authors, The HuggingFace Inc. team and Daniel Stancl. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Flax Bart model. """

import math
import random
from re import I
from typing import Callable, Optional, Tuple

import flax.linen as nn
import jax
import jax.numpy as jnp
from flax.core.frozen_dict import FrozenDict, unfreeze
from flax.linen import combine_masks, make_causal_mask
from flax.linen.attention import dot_product_attention_weights
from jax import lax
from jax.random import PRNGKey

from ...file_utils import add_start_docstrings, add_start_docstrings_to_model_forward
from ...modeling_flax_outputs import (
    FlaxBaseModelOutput,
    FlaxBaseModelOutputWithPastAndCrossAttentions,
    FlaxSeq2SeqLMOutput,
    FlaxSeq2SeqModelOutput,
    FlaxSeq2SeqQuestionAnsweringModelOutput,
    FlaxSeq2SeqSequenceClassifierOutput,
)
from ...modeling_flax_utils import ACT2FN, FlaxPreTrainedModel
from ...utils import logging
from .configuration_bart import BartConfig


logger = logging.get_logger(__name__)

_CONFIG_FOR_DOC = "BartConfig"
_TOKENIZER_FOR_DOC = "BartTokenizer"


BART_START_DOCSTRING = r"""
    This model inherits from :class:`~transformers.FlaxPreTrainedModel`. Check the superclass documentation for the
    generic methods the library implements for all its model (such as downloading, saving and converting weights from
    PyTorch models) This model is also a Flax Linen `flax.nn.Module
    <https://flax.readthedocs.io/en/latest/_autosummary/flax.nn.module.html>`__ subclass. Use it as a regular Flax
    Module and refer to the Flax documentation for all matter related to general usage and behavior. Finally, this
    model supports inherent JAX features such as:

    - `Just-In-Time (JIT) compilation <https://jax.readthedocs.io/en/latest/jax.html#just-in-time-compilation-jit>`__
    - `Automatic Differentiation <https://jax.readthedocs.io/en/latest/jax.html#automatic-differentiation>`__
    - `Vectorization <https://jax.readthedocs.io/en/latest/jax.html#vectorization-vmap>`__
    - `Parallelization <https://jax.readthedocs.io/en/latest/jax.html#parallelization-pmap>`__

    Parameters:
        config (:class:`~transformers.BartConfig`): Model configuration class with all the parameters of the model.
            Initializing with a config file does not load the weights associated with the model, only the
            configuration. Check out the :meth:`~transformers.PreTrainedModel.from_pretrained` method to load the model
            weights.
"""

BART_INPUTS_DOCSTRING = r"""
    Args:
        input_ids (:obj:`jnp.ndarray` of shape :obj:`(batch_size, sequence_length)`):
            Indices of input sequence tokens in the vocabulary. Padding will be ignored by default should you provide
            it.

            Indices can be obtained using :class:`~transformers.BartTokenizer`. See
            :meth:`transformers.PreTrainedTokenizer.encode` and :meth:`transformers.PreTrainedTokenizer.__call__` for
            details.

            `What are input IDs? <../glossary.html#input-ids>`__
        attention_mask (:obj:`jnp.ndarray` of shape :obj:`(batch_size, sequence_length)`, `optional`):
            Mask to avoid performing attention on padding token indices. Mask values selected in ``[0, 1]``:

            - 1 for tokens that are **not masked**,
            - 0 for tokens that are **masked**.

            `What are attention masks? <../glossary.html#attention-mask>`__
        decoder_input_ids (:obj:`jnp.ndarray` of shape :obj:`(batch_size, target_sequence_length)`, `optional`):
            Indices of decoder input sequence tokens in the vocabulary.

            Indices can be obtained using :class:`~transformers.BartTokenizer`. See
            :meth:`transformers.PreTrainedTokenizer.encode` and :meth:`transformers.PreTrainedTokenizer.__call__` for
            details.

            `What are decoder input IDs? <../glossary.html#decoder-input-ids>`__

            Bart uses the :obj:`eos_token_id` as the starting token for :obj:`decoder_input_ids` generation. If
            :obj:`past_key_values` is used, optionally only the last :obj:`decoder_input_ids` have to be input (see
            :obj:`past_key_values`).

            For translation and summarization training, :obj:`decoder_input_ids` should be provided. If no
            :obj:`decoder_input_ids` is provided, the model will create this tensor by shifting the :obj:`input_ids` to
            the right for denoising pre-training following the paper.
        decoder_attention_mask (:obj:`jnp.ndarray` of shape :obj:`(batch_size, target_sequence_length)`, `optional`):
            Default behavior: generate a tensor that ignores pad tokens in :obj:`decoder_input_ids`. Causal mask will
            also be used by default.

            If you want to change padding behavior, you should read :func:`modeling_bart._prepare_decoder_inputs` and
            modify to your needs. See diagram 1 in `the paper <https://arxiv.org/abs/1910.13461>`__ for more
            information on the default strategy.
        head_mask (:obj:`jnp.ndarray` of shape :obj:`(encoder_layers, encoder_attention_heads)`, `optional`):
            Mask to nullify selected heads of the attention modules in the encoder. Mask values selected in ``[0, 1]``:

            - 1 indicates the head is **not masked**,
            - 0 indicates the head is **masked**.

        decoder_head_mask (:obj:`jnp.ndarray` of shape :obj:`(decoder_layers, decoder_attention_heads)`, `optional`):
            Mask to nullify selected heads of the attention modules in the decoder. Mask values selected in ``[0, 1]``:

            - 1 indicates the head is **not masked**,
            - 0 indicates the head is **masked**.

        cross_attn_head_mask (:obj:`jnp.ndarray` of shape :obj:`(decoder_layers, decoder_attention_heads)`, `optional`):
            Mask to nullify selected heads of the cross-attention modules in the decoder. Mask values selected in ``[0,
            1]``:

            - 1 indicates the head is **not masked**,
            - 0 indicates the head is **masked**.

        encoder_outputs (:obj:`tuple(tuple(jnp.ndarray)`, `optional`):
            Tuple consists of (:obj:`last_hidden_state`, `optional`: :obj:`hidden_states`, `optional`:
            :obj:`attentions`) :obj:`last_hidden_state` of shape :obj:`(batch_size, sequence_length, hidden_size)`,
            `optional`) is a sequence of hidden-states at the output of the last layer of the encoder. Used in the
            cross-attention of the decoder.
        past_key_values (:obj:`Tuple[Tuple[jnp.ndarray]]` of length :obj:`config.n_layers` with each tuple having 2 tuples each of which has 2 tensors of shape :obj:`(batch_size, num_heads, sequence_length - 1, embed_size_per_head)`):
            Contains precomputed key and value hidden-states of the attention blocks. Can be used to speed up decoding.

            If :obj:`past_key_values` are used, the user can optionally input only the last :obj:`decoder_input_ids`
            (those that don't have their past key value states given to this model) of shape :obj:`(batch_size, 1)`
            instead of all :obj:`decoder_input_ids`` of shape :obj:`(batch_size, sequence_length)`.
        inputs_embeds (:obj:`jnp.ndarray` of shape :obj:`(batch_size, sequence_length, hidden_size)`, `optional`):
            Optionally, instead of passing :obj:`input_ids` you can choose to directly pass an embedded representation.
            This is useful if you want more control over how to convert :obj:`input_ids` indices into associated
            vectors than the model's internal embedding lookup matrix.
        decoder_inputs_embeds (:obj:`jnp.ndarray` of shape :obj:`(batch_size, target_sequence_length, hidden_size)`, `optional`):
            Optionally, instead of passing :obj:`decoder_input_ids` you can choose to directly pass an embedded
            representation. If :obj:`past_key_values` is used, optionally only the last :obj:`decoder_inputs_embeds`
            have to be input (see :obj:`past_key_values`). This is useful if you want more control over how to convert
            :obj:`decoder_input_ids` indices into associated vectors than the model's internal embedding lookup matrix.

            If :obj:`decoder_input_ids` and :obj:`decoder_inputs_embeds` are both unset, :obj:`decoder_inputs_embeds`
            takes the value of :obj:`inputs_embeds`.
        use_cache (:obj:`bool`, `optional`):
            If set to :obj:`True`, :obj:`past_key_values` key value states are returned and can be used to speed up
            decoding (see :obj:`past_key_values`).
        output_attentions (:obj:`bool`, `optional`):
            Whether or not to return the attentions tensors of all attention layers. See ``attentions`` under returned
            tensors for more detail.
        output_hidden_states (:obj:`bool`, `optional`):
            Whether or not to return the hidden states of all layers. See ``hidden_states`` under returned tensors for
            more detail.
        return_dict (:obj:`bool`, `optional`):
            Whether or not to return a :class:`~transformers.file_utils.ModelOutput` instead of a plain tuple.
"""


def shift_tokens_right(input_ids: jnp.ndarray, pad_token_id: int, decoder_start_token_id: int) -> jnp.ndarray:
    """
    Shift input ids one token to the right.
    """
    shifted_input_ids = jnp.roll(input_ids, 1, axis=-1)
    shifted_input_ids = jax.ops.index_update(shifted_input_ids, (..., 0), decoder_start_token_id)
    # replace possible -100 values in labels by `pad_token_id`
    shifted_input_ids = jax.ops.index_update(shifted_input_ids, shifted_input_ids == -100, pad_token_id)

    return shifted_input_ids


class FlaxBartAttention(nn.Module):
    config: BartConfig
    embed_dim: int
    num_heads: int
    dropout: float = 0.0
    is_decoder: bool = False
    causal: bool = False
    bias: bool = True
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self) -> None:
        self.head_dim = self.embed_dim // self.num_heads
        assert (
            self.head_dim * self.num_heads == self.embed_dim
        ), f"embed_dim must be divisible by num_heads (got `embed_dim`: {self.embed_dim} and `num_heads`: {self.num_heads})."

        self.k_proj = nn.Dense(
            self.embed_dim,
            use_bias=self.bias,
            dtype=self.dtype,
            kernel_init=jax.nn.initializers.normal(self.config.init_std, self.dtype),
        )
        self.v_proj = nn.Dense(
            self.embed_dim,
            use_bias=self.bias,
            dtype=self.dtype,
            kernel_init=jax.nn.initializers.normal(self.config.init_std, self.dtype),
        )
        self.q_proj = nn.Dense(
            self.embed_dim,
            use_bias=self.bias,
            dtype=self.dtype,
            kernel_init=jax.nn.initializers.normal(self.config.init_std, self.dtype),
        )
        self.out_proj = nn.Dense(
            self.embed_dim,
            use_bias=self.bias,
            dtype=self.dtype,
            kernel_init=jax.nn.initializers.normal(self.config.init_std, self.dtype),
        )

        self.dropout_layer = nn.Dropout(rate=self.dropout)

        if self.causal:
            self.causal_mask = make_causal_mask(
                jnp.ones((1, self.config.max_position_embeddings), dtype="bool"), dtype="bool"
            )

    def _split_heads(self, hidden_states):
        return hidden_states.reshape(hidden_states.shape[:2] + (self.num_heads, self.head_dim))

    def _merge_heads(self, hidden_states):
        return hidden_states.reshape(hidden_states.shape[:2] + (self.embed_dim,))

    @nn.compact
    def _concatenate_to_cache(self, key, value, query, attention_mask):
        """
        This function takes projected key, value states from a single input token and concatenates the states to cached
        states from previous steps. This function is slighly adapted from the official Flax repository:
        https://github.com/google/flax/blob/491ce18759622506588784b4fca0e4bf05f8c8cd/flax/linen/attention.py#L252
        """
        # detect if we're initializing by absence of existing cache data.
        is_initialized = self.has_variable("cache", "cached_key")
        cached_key = self.variable("cache", "cached_key", jnp.zeros, key.shape, key.dtype)
        cached_value = self.variable("cache", "cached_value", jnp.zeros, value.shape, value.dtype)
        cache_index = self.variable("cache", "cache_index", lambda: jnp.array(0, dtype=jnp.int32))

        if is_initialized:
            *batch_dims, max_length, num_heads, depth_per_head = cached_key.value.shape
            # update key, value caches with our new 1d spatial slices
            cur_index = cache_index.value
            indices = (0,) * len(batch_dims) + (cur_index, 0, 0)
            key = lax.dynamic_update_slice(cached_key.value, key, indices)
            value = lax.dynamic_update_slice(cached_value.value, value, indices)
            cached_key.value = key
            cached_value.value = value
            num_updated_cache_vectors = query.shape[1]
            cache_index.value = cache_index.value + num_updated_cache_vectors
            # causal mask for cached decoder self-attention: our single query position should only attend to those key positions that have already been generated and cached, not the remaining zero elements.
            pad_mask = jnp.broadcast_to(
                jnp.arange(max_length) < cur_index + num_updated_cache_vectors,
                tuple(batch_dims) + (1, num_updated_cache_vectors, max_length),
            )
            attention_mask = combine_masks(pad_mask, attention_mask)
        return key, value, attention_mask

    def __call__(
        self,
        hidden_states: jnp.ndarray,
        key_value_states: Optional[jnp.ndarray] = None,
        attention_mask: Optional[jnp.ndarray] = None,
        init_cache: bool = False,
        output_attentions: bool = False,
        deterministic: bool = True,
    ) -> Tuple[jnp.ndarray]:
        """Input shape: Batch x Time x Channel"""

        # if key_value_states are provided this layer is used as a cross-attention layer
        # for the decoder
        is_cross_attention = key_value_states is not None
        batch_size, tgt_len, embed_dim = hidden_states.shape

        # get query proj
        query_states = self.q_proj(hidden_states)
        # get key, value proj
        if is_cross_attention:
            # cross_attentions
            key_states = self.k_proj(key_value_states)
            value_states = self.v_proj(key_value_states)
        else:
            # self_attention
            key_states = self.k_proj(hidden_states)
            value_states = self.v_proj(hidden_states)

        query_states = self._split_heads(query_states)
        key_states = self._split_heads(key_states)
        value_states = self._split_heads(value_states)

        # handle cache prepare causal attention mask
        if self.causal:
            query_length, key_length = query_states.shape[1], key_states.shape[1]
            if self.has_variable("cache", "cached_key"):
                mask_shift = self.variables["cache"]["cache_index"]
                max_decoder_length = self.variables["cache"]["cached_key"].shape[1]
                causal_mask = lax.dynamic_slice(
                    self.causal_mask, (0, 0, mask_shift, 0), (1, 1, query_length, max_decoder_length)
                )
            else:
                causal_mask = self.causal_mask[:, :, :query_length, :key_length]
            causal_mask = jnp.broadcast_to(causal_mask, (batch_size,) + causal_mask.shape[1:])

        # combine masks if needed
        if attention_mask is not None and self.causal:
            attention_mask = jnp.broadcast_to(jnp.expand_dims(attention_mask, axis=(-3, -2)), causal_mask.shape)
            attention_mask = combine_masks(attention_mask, causal_mask)
        elif self.causal:
            attention_mask = causal_mask
        elif attention_mask is not None:
            attention_mask = jnp.expand_dims(attention_mask, axis=(-3, -2))

        # During fast autoregressive decoding, we feed one position at a time,
        # and cache the keys and values step by step.
        if self.causal and (self.has_variable("cache", "cached_key") or init_cache):
            key_states, value_states, attention_mask = self._concatenate_to_cache(
                key_states, value_states, query_states, attention_mask
            )

        # Convert the boolean attention mask to an attention bias.
        if attention_mask is not None:
            # attention mask in the form of attention bias
            attention_bias = lax.select(
                attention_mask > 0,
                jnp.full(attention_mask.shape, 0.0).astype(self.dtype),
                jnp.full(attention_mask.shape, float("-inf")).astype(self.dtype),
            )
        else:
            attention_bias = None

        dropout_rng = None
        if not deterministic and self.dropout > 0.0:
            dropout_rng = self.make_rng("dropout")

        attn_weights = dot_product_attention_weights(
            query_states,
            key_states,
            bias=attention_bias,
            dropout_rng=dropout_rng,
            dropout_rate=self.dropout,
            broadcast_dropout=True,
            deterministic=deterministic,
            dtype=self.dtype,
            precision=None,
        )

        attn_output = jnp.einsum("...hqk,...khd->...qhd", attn_weights, value_states)
        attn_output = self._merge_heads(attn_output)
        attn_output = self.out_proj(attn_output)

        return attn_output, attn_weights


class FlaxBartEncoderLayer(nn.Module):
    config: BartConfig
    dtype: jnp.dtype = jnp.float32

    def setup(self) -> None:
        self.embed_dim = self.config.d_model
        self.self_attn = FlaxBartAttention(
            config=self.config,
            embed_dim=self.embed_dim,
            num_heads=self.config.encoder_attention_heads,
            dropout=self.config.attention_dropout,
        )
        self.self_attn_layer_norm = nn.LayerNorm(dtype=self.dtype)
        self.dropout_layer = nn.Dropout(rate=self.config.dropout)
        self.activation_fn = ACT2FN[self.config.activation_function]
        self.acticvation_dropout_layer = nn.Dropout(rate=self.config.activation_dropout)
        self.fc1 = nn.Dense(self.config.encoder_ffn_dim)
        self.fc2 = nn.Dense(self.embed_dim)
        self.final_layer_norm = nn.LayerNorm(dtype=self.dtype)

    def __call__(
        self,
        hidden_states: jnp.ndarray,
        attention_mask: jnp.ndarray,
        output_attentions: bool = True,
        deterministic: bool = True,
    ) -> Tuple[jnp.ndarray]:
        """
        Args:
            hidden_states (:obj:`jnp.ndarray`): input to the layer of shape `(seq_len, batch, embed_dim)`
            attention_mask (:obj:`jnp.ndarray`): attention mask of size
                `(batch, 1, tgt_len, src_len)` where padding elements are indicated by very large negative values.
            layer_head_mask (:obj:`jnp.ndarray`): mask for attention heads in a given layer of size
                `(encoder_attention_heads,)`.
            output_attentions (:obj:`bool`, `optional`):
                Whether or not to return the attentions tensors of all attention layers. See ``attentions`` under
                returned tensors for more detail.
        """
        residual = hidden_states
        hidden_states, attn_weights = self.self_attn(
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            output_attentions=output_attentions,
        )

        hidden_states = self.dropout_layer(hidden_states, deterministic=deterministic)
        hidden_states = residual + hidden_states
        hidden_states = self.self_attn_layer_norm(hidden_states)

        residual = hidden_states
        hidden_states = self.activation_fn(self.fc1(hidden_states))
        hidden_states = self.acticvation_dropout_layer(hidden_states, deterministic=deterministic)
        hidden_states = self.fc2(hidden_states)
        hidden_states = self.dropout_layer(hidden_states, deterministic=deterministic)
        hidden_states = residual + hidden_states
        hidden_states = self.final_layer_norm(hidden_states)

        outputs = (hidden_states,)

        if output_attentions:
            outputs += (attn_weights,)

        return outputs


class FlaxBartEncoderLayerCollection(nn.Module):
    config: BartConfig
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self):
        self.layers = [
            FlaxBartEncoderLayer(self.config, name=str(i), dtype=self.dtype) for i in range(self.config.encoder_layers)
        ]
        self.layerdrop = self.config.encoder_layerdrop

    def __call__(
        self,
        hidden_states,
        attention_mask,
        deterministic: bool = True,
        output_attentions: bool = False,
        output_hidden_states: bool = False,
        return_dict: bool = True,
    ):
        all_attentions = () if output_attentions else None
        all_hidden_states = () if output_hidden_states else None

        for idx, encoder_layer in enumerate(self.layers):
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)
            # add LayerDrop (see https://arxiv.org/abs/1909.11556 for description)
            dropout_probability = random.uniform(0, 1)
            if deterministic and (dropout_probability < self.layerdrop):  # skip the layer
                hidden_states, attn = (None, None)

            hidden_states, attn = encoder_layer(
                hidden_states,
                attention_mask,
                True,  # we want to always output attentions at this step (at least so far for debugging purposes :) )
                deterministic,
            )

            if output_attentions:
                all_attentions = all_attentions + (attn,)

        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        outputs = (hidden_states, all_hidden_states, all_attentions)

        if not return_dict:
            return tuple(v for v in outputs if v is not None)

        return FlaxBaseModelOutput(
            last_hidden_state=hidden_states, hidden_states=all_hidden_states, attentions=all_attentions
        )


class FlaxBartDecoderLayer(nn.Module):
    config: BartConfig
    dtype: jnp.dtype = jnp.float32

    def setup(self) -> None:
        self.embed_dim = self.config.d_model
        self.self_attn = FlaxBartAttention(
            config=self.config,
            embed_dim=self.embed_dim,
            num_heads=self.config.decoder_attention_heads,
            dropout=self.config.attention_dropout,
            is_decoder=True,
            causal=True,
        )
        self.dropout_layer = nn.Dropout(rate=self.config.dropout)
        self.activation_fn = ACT2FN[self.config.activation_function]
        self.acticvation_dropout_layer = nn.Dropout(rate=self.config.activation_dropout)

        self.self_attn_layer_norm = nn.LayerNorm(dtype=self.dtype)
        self.encoder_attn = FlaxBartAttention(
            config=self.config,
            embed_dim=self.embed_dim,
            num_heads=self.config.decoder_attention_heads,
            dropout=self.config.attention_dropout,
            is_decoder=True,
        )
        self.encoder_attn_layer_norm = nn.LayerNorm(dtype=self.dtype)
        self.fc1 = nn.Dense(self.config.encoder_ffn_dim)
        self.fc2 = nn.Dense(self.embed_dim)
        self.final_layer_norm = nn.LayerNorm(dtype=self.dtype)

    def __call__(
        self,
        hidden_states: jnp.ndarray,
        attention_mask: Optional[jnp.ndarray] = None,
        encoder_hidden_states: Optional[jnp.ndarray] = None,
        encoder_attention_mask: Optional[jnp.ndarray] = None,
        init_cache: bool = False,
        output_attentions: bool = True,
        use_cache: bool = True,
        deterministic: bool = True,
    ) -> Tuple[jnp.ndarray]:
        """
        Args:
            hidden_states (:obj:`jnp.ndarray`): input to the layer of shape `(seq_len, batch, embed_dim)`
            attention_mask (:obj:`jnp.ndarray`): attention mask of size
                `(batch, 1, tgt_len, src_len)` where padding elements are indicated by very large negative values.
            encoder_hidden_states (:obj:`jnp.ndarray`): cross attention input to the layer of shape `(seq_len, batch, embed_dim)`
            encoder_attention_mask (:obj:`jnp.ndarray`): encoder attention mask of size
                `(batch, 1, tgt_len, src_len)` where padding elements are indicated by very large negative values.
            layer_head_mask (:obj:`jnp.ndarray`): mask for attention heads in a given layer of size
                `(encoder_attention_heads,)`.
            cross_attn_layer_head_mask (:obj:`jnp.ndarray`): mask for cross-attention heads in a given layer of
                size `(decoder_attention_heads,)`.
            past_key_value (:obj:`Tuple(jnp.ndarray)`): cached past key and value projection states
            output_attentions (:obj:`bool`, `optional`):
                Whether or not to return the attentions tensors of all attention layers. See ``attentions`` under
                returned tensors for more detail.
        """
        residual = hidden_states

        # Self Attention
        hidden_states, self_attn_weights = self.self_attn(
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            init_cache=init_cache,
            output_attentions=output_attentions,
        )
        hidden_states = self.dropout_layer(hidden_states, deterministic=deterministic)
        hidden_states = residual + hidden_states
        hidden_states = self.self_attn_layer_norm(hidden_states)

        # Cross-Attention Block
        cross_attn_weights = None
        if encoder_hidden_states is not None:
            residual = hidden_states

            hidden_states, cross_attn_weights = self.encoder_attn(
                hidden_states=hidden_states,
                key_value_states=encoder_hidden_states,
                attention_mask=encoder_attention_mask,
                output_attentions=output_attentions,
            )
            hidden_states = self.dropout_layer(hidden_states, deterministic=deterministic)
            hidden_states = residual + hidden_states
            hidden_states = self.encoder_attn_layer_norm(hidden_states)

        # Fully Connected
        residual = hidden_states
        hidden_states = self.activation_fn(self.fc1(hidden_states))
        hidden_states = self.acticvation_dropout_layer(hidden_states, deterministic=deterministic)
        hidden_states = self.fc2(hidden_states)
        hidden_states = self.dropout_layer(hidden_states, deterministic=deterministic)
        hidden_states = residual + hidden_states
        hidden_states = self.final_layer_norm(hidden_states)

        outputs = (hidden_states,)

        if output_attentions:
            outputs += (self_attn_weights, cross_attn_weights)

        return outputs


class FlaxBartDecoderLayerCollection(nn.Module):
    config: BartConfig
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self):
        self.layers = [
            FlaxBartDecoderLayer(self.config, name=str(i), dtype=self.dtype) for i in range(self.config.decoder_layers)
        ]
        self.layerdrop = self.config.encoder_layerdrop

    def __call__(
        self,
        hidden_states,
        attention_mask,
        encoder_hidden_states,
        encoder_attention_mask,
        deterministic: bool = True,
        init_cache: bool = False,
        output_attentions: bool = False,
        output_hidden_states: bool = False,
        use_cache: bool = True,
        return_dict: bool = True,
    ):
        # decoder layers
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None
        all_cross_attentions = () if (output_attentions and encoder_hidden_states is not None) else None

        for idx, decoder_layer in enumerate(self.layers):
            if output_hidden_states:
                all_hidden_states += (hidden_states,)
                # add LayerDrop (see https://arxiv.org/abs/1909.11556 for description)
            dropout_probability = random.uniform(0, 1)
            if deterministic and (dropout_probability < self.layerdrop):
                continue

            layer_outputs = decoder_layer(
                hidden_states,
                attention_mask=attention_mask,
                encoder_hidden_states=encoder_hidden_states,
                encoder_attention_mask=encoder_attention_mask,
                init_cache=init_cache,
                output_attentions=output_attentions,
                use_cache=use_cache,
                deterministic=deterministic,
            )
            hidden_states = layer_outputs[0]

            if output_attentions:
                all_self_attns += (layer_outputs[1],)

                if encoder_hidden_states is not None:
                    all_cross_attentions += (layer_outputs[2],)

        # add hidden states from the last decoder layer
        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        outputs = [hidden_states, all_hidden_states, all_self_attns, all_cross_attentions]

        if not return_dict:
            return tuple(v for v in outputs if v is not None)

        return FlaxBaseModelOutputWithPastAndCrossAttentions(
            last_hidden_state=hidden_states,
            hidden_states=all_hidden_states,
            attentions=all_self_attns,
            cross_attentions=all_cross_attentions,
        )


class FlaxBartClassificationHead(nn.Module):
    """Head for sentence-level classification tasks."""

    config: BartConfig
    inner_dim: int
    num_classes: int
    pooler_dropout: float
    dtype: jnp.dtype = jnp.float32

    def setup(self):
        self.dense = nn.Dense(self.inner_dim, dtype=self.dtype)
        self.dropout = nn.Dropout(rate=self.pooler_dropout)
        self.out_proj = nn.Dense(self.num_classes, dtype=self.dtype)

    def __call__(self, hidden_states: jnp.ndarray, deterministic: bool):
        hidden_states = self.dropout(hidden_states, deterministic=deterministic)
        hidden_states = self.dense(hidden_states)
        hidden_states = jnp.tanh(hidden_states)
        hidden_states = self.dropout(hidden_states, deterministic=deterministic)
        hidden_states = self.out_proj(hidden_states)
        return hidden_states


class FlaxBartPretrainedModel(FlaxPreTrainedModel):
    config_class = BartConfig
    base_model_prefix: str = "bart"
    module_class: nn.Module = None

    def __init__(
        self,
        config: BartConfig,
        input_shape: Tuple[int] = (1, 1),
        seed: int = 0,
        dtype: jnp.dtype = jnp.float32,
        **kwargs
    ):
        module = self.module_class(config=config, dtype=dtype, **kwargs)
        super().__init__(config, module, input_shape=input_shape, seed=seed, dtype=dtype)

    def init_weights(self, rng: jax.random.PRNGKey, input_shape: Tuple) -> FrozenDict:
        # init input tensors
        input_ids = jnp.zeros(input_shape, dtype="i4")
        input_ids = jax.ops.index_update(
            input_ids, (..., -1), self.config.eos_token_id
        )  # make sure initialization pass will work
        attention_mask = jnp.ones_like(input_ids)
        decoder_input_ids = input_ids
        decoder_attention_mask = jnp.ones_like(input_ids)

        batch_size, sequence_length = input_ids.shape
        position_ids = jnp.broadcast_to(jnp.arange(sequence_length)[None, :], (batch_size, sequence_length))
        decoder_position_ids = jnp.broadcast_to(jnp.arange(sequence_length)[None, :], (batch_size, sequence_length))

        params_rng, dropout_rng = jax.random.split(rng)
        rngs = {"params": params_rng, "dropout": dropout_rng}

        return self.module.init(
            rngs,
            input_ids,
            attention_mask,
            decoder_input_ids,
            decoder_attention_mask,
            position_ids,
            decoder_position_ids,
        )["params"]

    def init_cache(self, batch_size, max_length, encoder_outputs):
        r"""
        Args:
            batch_size (:obj:`int`):
                batch_size used for fast auto-regressive decoding. Defines the batch size of the initialized cache.
            max_length (:obj:`int`):
                maximum possible length for auto-regressive decoding. Defines the sequence length of the initialized
                cache.
        """
        # init input variables to retrieve cache
        decoder_input_ids = jnp.ones((batch_size, max_length), dtype="i4")
        decoder_attention_mask = jnp.ones_like(decoder_input_ids)
        decoder_position_ids = jnp.broadcast_to(
            jnp.arange(jnp.atleast_2d(decoder_input_ids).shape[-1]), decoder_input_ids.shape
        )

        encoder_hidden_states = encoder_outputs[0]
        batch_size, seq_length, _ = encoder_hidden_states.shape
        encoder_attention_mask = jnp.ones((batch_size, seq_length), dtype="i4")

        init_variables = self.module.init(
            jax.random.PRNGKey(0),
            decoder_input_ids,
            decoder_attention_mask,
            decoder_position_ids,
            encoder_hidden_states,
            encoder_attention_mask,
            init_cache=True,
            method=self.module.decode,  # we just need to run the decoder, expects the module to have decode method defined
        )
        return unfreeze(init_variables["cache"])

    # TODO (PS): better name ?
    def encoder(
        self,
        input_ids: jnp.ndarray,
        attention_mask: Optional[jnp.ndarray] = None,
        position_ids: Optional[jnp.ndarray] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: bool = True,
        deterministic: bool = True,
        params: dict = None,
        dropout_rng: PRNGKey = None,
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.return_dict

        if attention_mask is None:
            attention_mask = jnp.ones_like(input_ids)
        if position_ids is None:
            batch_size, sequence_length = input_ids.shape
            position_ids = jnp.broadcast_to(jnp.arange(sequence_length)[None, :], (batch_size, sequence_length))

        # Handle any PRNG if needed
        rngs = {}
        if dropout_rng is not None:
            rngs["dropout"] = dropout_rng

        def _encoder_forward(module, input_ids, attention_mask, position_ids, **kwargs):
            return module.get_encoder()(input_ids, attention_mask, position_ids, **kwargs)

        return self.module.apply(
            {"params": params or self.params},
            input_ids=jnp.array(input_ids, dtype="i4"),
            attention_mask=jnp.array(attention_mask, dtype="i4"),
            position_ids=jnp.array(position_ids, dtype="i4"),
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            deterministic=deterministic,
            rngs=rngs,
            method=_encoder_forward,
        )

    def __call__(
        self,
        input_ids: Optional[jnp.ndarray] = None,
        attention_mask: Optional[jnp.ndarray] = None,
        decoder_input_ids: Optional[jnp.ndarray] = None,
        decoder_attention_mask: Optional[jnp.ndarray] = None,
        position_ids: Optional[jnp.ndarray] = None,
        decoder_position_ids: Optional[jnp.ndarray] = None,
        encoder_outputs: Optional[FlaxBaseModelOutput] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        deterministic: bool = True,
        params: dict = None,
        past_key_values: dict = None,
        dropout_rng: PRNGKey = None,
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.return_dict

        if input_ids is None and encoder_outputs is None:
            raise ValueError("You have to specify either input_ids or encoder_outputs.")

        if input_ids is None and decoder_input_ids is None:
            raise ValueError(
                "Can not prepare decoder_input_ids. You have to specify either input_ids or decoder_input_ids."
            )

        # prepare encoder inputs
        if encoder_outputs is not None:
            batch_size, sequence_length = encoder_outputs[0].shape[:2]
            attention_mask = jnp.ones((batch_size, sequence_length), dtype="i4")
            position_ids = None
        else:
            if attention_mask is None:
                attention_mask = jnp.ones_like(input_ids)
            if position_ids is None:
                batch_size, sequence_length = input_ids.shape
                position_ids = jnp.broadcast_to(jnp.arange(sequence_length)[None, :], (batch_size, sequence_length))

            # ensure everything is a jnp array
            input_ids = jnp.array(input_ids, dtype="i4")
            attention_mask = jnp.array(attention_mask, dtype="i4")
            position_ids = jnp.array(position_ids, dtype="i4")

        # prepare decoder inputs
        if decoder_input_ids is None:
            decoder_input_ids = shift_tokens_right(
                input_ids, self.config.pad_token_id, decoder_start_token_id=self.config.decoder_start_token_id
            )
        if decoder_attention_mask is None:
            decoder_attention_mask = jnp.ones_like(decoder_input_ids)
        if decoder_position_ids is None:
            batch_size, sequence_length = decoder_input_ids.shape
            decoder_position_ids = jnp.broadcast_to(
                jnp.arange(sequence_length)[None, :], (batch_size, sequence_length)
            )

        # Handle any PRNG if needed
        rngs = {}
        if dropout_rng is not None:
            rngs["dropout"] = dropout_rng

        inputs = {"params": params or self.params}

        # if past_key_values are passed then cache is already initialized a private flag init_cache has to be
        # passed down to ensure cache is used. It has to be made sure that cache is marked as mutable so that
        # it can be changed by FlaxBartAttention module
        if past_key_values:
            inputs["cache"] = past_key_values
            mutable = ["cache"]
        else:
            mutable = False

        outputs = self.module.apply(
            inputs,
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            decoder_input_ids=jnp.array(decoder_input_ids, dtype="i4"),
            decoder_attention_mask=jnp.array(decoder_attention_mask, dtype="i4"),
            decoder_position_ids=jnp.array(decoder_position_ids, dtype="i4"),
            encoder_outputs=encoder_outputs,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            deterministic=deterministic,
            rngs=rngs,
            mutable=mutable,
        )

        # add updated cache to model output
        if past_key_values is not None and return_dict:
            outputs, past_key_values = outputs
            outputs["past_key_values"] = unfreeze(past_key_values["cache"])
            return outputs
        elif past_key_values is not None and not return_dict:
            outputs, past_key_values = outputs
            outputs = outputs[:1] + (unfreeze(past_key_values["cache"]),) + outputs[1:]

        return outputs


class FlaxBartEncoder(nn.Module):
    """
    Transformer encoder consisting of *config.encoder_layers* self attention layers. Each layer is a
    :class:`FlaxBartEncoderLayer`.

    Args:
        config: BartConfig
        embed_tokens (flax.linen.Embedding): output embedding
    """

    config: BartConfig
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation
    embed_tokens: Optional[nn.Embed] = None

    def setup(self):
        self.dropout_layer = nn.Dropout(rate=self.config.dropout)
        self.layerdrop = self.config.encoder_layerdrop

        embed_dim = self.config.d_model
        self.padding_idx = self.config.pad_token_id
        self.max_source_positions = self.config.max_position_embeddings
        self.embed_scale = math.sqrt(embed_dim) if self.config.scale_embedding else 1.0

        if self.embed_tokens is None:
            self.embed_tokens = nn.Embed(
                self.config.vocab_size,
                embed_dim,
                embedding_init=jax.nn.initializers.normal(self.config.init_std, self.dtype),
                dtype=self.dtype,
            )

        # Bart is set up so that if padding_idx is specified then offset the embedding ids by 2
        # and adjust num_embeddings appropriately. Other models don't have this hack
        self.offset = 2
        self.embed_positions = nn.Embed(
            self.config.max_position_embeddings + self.offset,
            embed_dim,
            embedding_init=jax.nn.initializers.normal(self.config.init_std, self.dtype),
            dtype=self.dtype,
        )
        self.layers = FlaxBartEncoderLayerCollection(self.config, self.dtype)
        self.layernorm_embedding = nn.LayerNorm(dtype=self.dtype)

    def __call__(
        self,
        input_ids,
        attention_mask,
        position_ids,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: bool = True,
        deterministic: bool = True,
    ):
        r"""
        Args:
            input_ids (:obj:`jnp.ndarray` of shape :obj:`(batch_size, sequence_length)`):
                Indices of input sequence tokens in the vocabulary. Padding will be ignored by default should you
                provide it.

                Indices can be obtained using :class:`~transformers.BartTokenizer`. See
                :meth:`transformers.PreTrainedTokenizer.encode` and :meth:`transformers.PreTrainedTokenizer.__call__`
                for details.

                `What are input IDs? <../glossary.html#input-ids>`__
            attention_mask (:obj:`jnp.ndarray` of shape :obj:`(batch_size, sequence_length)`, `optional`):
                Mask to avoid performing attention on padding token indices. Mask values selected in ``[0, 1]``:

                - 1 for tokens that are **not masked**,
                - 0 for tokens that are **masked**.

                `What are attention masks? <../glossary.html#attention-mask>`__
            head_mask (:obj:`jnp.ndarray` of shape :obj:`(encoder_layers, encoder_attention_heads)`, `optional`):
                Mask to nullify selected heads of the attention modules. Mask values selected in ``[0, 1]``:

                - 1 indicates the head is **not masked**,
                - 0 indicates the head is **masked**.

            inputs_embeds (:obj:`jnp.ndarray` of shape :obj:`(batch_size, sequence_length, hidden_size)`, `optional`):
                Optionally, instead of passing :obj:`input_ids` you can choose to directly pass an embedded
                representation. This is useful if you want more control over how to convert :obj:`input_ids` indices
                into associated vectors than the model's internal embedding lookup matrix.
            output_attentions (:obj:`bool`, `optional`):
                Whether or not to return the attentions tensors of all attention layers. See ``attentions`` under
                returned tensors for more detail.
            output_hidden_states (:obj:`bool`, `optional`):
                Whether or not to return the hidden states of all layers. See ``hidden_states`` under returned tensors
                for more detail.
            return_dict (:obj:`bool`, `optional`):
                Whether or not to return a :class:`~transformers.file_utils.ModelOutput` instead of a plain tuple.
        """
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        input_shape = input_ids.shape
        input_ids = input_ids.reshape(-1, input_shape[-1])

        inputs_embeds = self.embed_tokens(input_ids) * self.embed_scale

        embed_pos = self.embed_positions(position_ids + self.offset)

        hidden_states = inputs_embeds + embed_pos
        hidden_states = self.layernorm_embedding(hidden_states)
        hidden_states = self.dropout_layer(hidden_states, deterministic=deterministic)

        outputs = self.layers(
            hidden_states,
            attention_mask,
            deterministic=deterministic,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        if not return_dict:
            return outputs

        return FlaxBaseModelOutput(
            last_hidden_state=outputs.last_hidden_state,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )


class FlaxBartDecoder(nn.Module):
    """
    Transformer decoder consisting of *config.decoder_layers* layers. Each layer is a :class:`FlaxBartDecoderLayer`

    Args:
        config: BartConfig
        embed_tokens (flax.linen.Embedding): output embedding
    """

    config: BartConfig
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation
    embed_tokens: Optional[nn.Embed] = None

    def setup(self):
        self.dropout_layer = nn.Dropout(rate=self.config.dropout)
        self.layerdrop = self.config.decoder_layerdrop

        embed_dim = self.config.d_model
        self.padding_idx = self.config.pad_token_id
        self.max_target_positions = self.config.max_position_embeddings
        self.embed_scale = math.sqrt(self.config.d_model) if self.config.scale_embedding else 1.0

        if self.embed_tokens is None:
            self.embed_tokens = nn.Embed(
                self.config.vocab_size,
                embed_dim,
                embedding_init=jax.nn.initializers.normal(self.config.init_std, self.dtype),
                dtype=self.dtype,
            )

        # Bart is set up so that if padding_idx is specified then offset the embedding ids by 2
        # and adjust num_embeddings appropriately. Other models don't have this hack
        self.offset = 2
        self.embed_positions = nn.Embed(
            self.config.max_position_embeddings + self.offset,
            embed_dim,
            embedding_init=jax.nn.initializers.normal(self.config.init_std, self.dtype),
            dtype=self.dtype,
        )

        self.layers = FlaxBartDecoderLayerCollection(self.config, self.dtype)
        self.layernorm_embedding = nn.LayerNorm(dtype=self.dtype)

    def get_input_embeddings(self):
        return self.embed_tokens

    def set_input_embeddings(self, value):
        self.embed_tokens = value

    def __call__(
        self,
        input_ids,
        attention_mask,
        position_ids,
        encoder_hidden_states: Optional[jnp.ndarray] = None,
        encoder_attention_mask: Optional[jnp.ndarray] = None,
        output_attentions: Optional[bool] = None,
        use_cache: Optional[bool] = None,
        init_cache: bool = False,
        output_hidden_states: Optional[bool] = None,
        return_dict: bool = True,
        deterministic: bool = True,
    ):
        r"""
        Args:
            input_ids (:obj:`jnp.ndarray` of shape :obj:`(batch_size, sequence_length)`):
                Indices of input sequence tokens in the vocabulary. Padding will be ignored by default should you
                provide it.

                Indices can be obtained using :class:`~transformers.BartTokenizer`. See
                :meth:`transformers.PreTrainedTokenizer.encode` and :meth:`transformers.PreTrainedTokenizer.__call__`
                for details.

                `What are input IDs? <../glossary.html#input-ids>`__
            attention_mask (:obj:`jnp.ndarray` of shape :obj:`(batch_size, sequence_length)`, `optional`):
                Mask to avoid performing attention on padding token indices. Mask values selected in ``[0, 1]``:

                - 1 for tokens that are **not masked**,
                - 0 for tokens that are **masked**.

                `What are attention masks? <../glossary.html#attention-mask>`__
            encoder_hidden_states (:obj:`jnp.ndarray` of shape :obj:`(batch_size, encoder_sequence_length, hidden_size)`, `optional`):
                Sequence of hidden-states at the output of the last layer of the encoder. Used in the cross-attention
                of the decoder.
            encoder_attention_mask (:obj:`jnp.ndarray` of shape :obj:`(batch_size, encoder_sequence_length)`, `optional`):
                Mask to avoid performing cross-attention on padding tokens indices of encoder input_ids. Mask values
                selected in ``[0, 1]``:

                - 1 for tokens that are **not masked**,
                - 0 for tokens that are **masked**.

                `What are attention masks? <../glossary.html#attention-mask>`__
            head_mask (:obj:`jnp.ndarray` of shape :obj:`(decoder_layers, decoder_attention_heads)`, `optional`):
                Mask to nullify selected heads of the attention modules. Mask values selected in ``[0, 1]``:

                - 1 indicates the head is **not masked**,
                - 0 indicates the head is **masked**.

            cross_attn_head_mask (:obj:`jnp.ndarray` of shape :obj:`(decoder_layers, decoder_attention_heads)`, `optional`):
                Mask to nullify selected heads of the cross-attention modules in the decoder to avoid performing
                cross-attention on hidden heads. Mask values selected in ``[0, 1]``:

                - 1 indicates the head is **not masked**,
                - 0 indicates the head is **masked**.

            past_key_values (:obj:`Tuple[Tuple[jnp.ndarray]]` of length :obj:`config.n_layers` with each tuple having 2 tuples each of which has 2 tensors of shape :obj:`(batch_size, num_heads, sequence_length - 1, embed_size_per_head)`):
                Contains precomputed key and value hidden-states of the attention blocks. Can be used to speed up
                decoding.

                If :obj:`past_key_values` are used, the user can optionally input only the last
                :obj:`decoder_input_ids` (those that don't have their past key value states given to this model) of
                shape :obj:`(batch_size, 1)` instead of all :obj:`decoder_input_ids`` of shape :obj:`(batch_size,
                sequence_length)`.
            inputs_embeds (:obj:`jnp.ndarray` of shape :obj:`(batch_size, sequence_length, hidden_size)`, `optional`):
                Optionally, instead of passing :obj:`input_ids` you can choose to directly pass an embedded
                representation. This is useful if you want more control over how to convert :obj:`input_ids` indices
                into associated vectors than the model's internal embedding lookup matrix.
            output_attentions (:obj:`bool`, `optional`):
                Whether or not to return the attentions tensors of all attention layers. See ``attentions`` under
                returned tensors for more detail.
            output_hidden_states (:obj:`bool`, `optional`):
                Whether or not to return the hidden states of all layers. See ``hidden_states`` under returned tensors
                for more detail.
            return_dict (:obj:`bool`, `optional`):
                Whether or not to return a :class:`~transformers.file_utils.ModelOutput` instead of a plain tuple.
        """
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        input_shape = input_ids.shape
        input_ids = input_ids.reshape(-1, input_shape[-1])

        inputs_embeds = self.embed_tokens(input_ids) * self.embed_scale

        # embed positions
        positions = self.embed_positions(position_ids + self.offset)

        hidden_states = inputs_embeds + positions
        hidden_states = self.layernorm_embedding(hidden_states)

        hidden_states = self.dropout_layer(hidden_states, deterministic=deterministic)

        outputs = self.layers(
            hidden_states,
            attention_mask,
            encoder_hidden_states,
            encoder_attention_mask,
            deterministic=deterministic,
            init_cache=init_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            use_cache=use_cache,
            return_dict=return_dict,
        )

        if not return_dict:
            return outputs

        return FlaxBaseModelOutputWithPastAndCrossAttentions(
            last_hidden_state=outputs.last_hidden_state,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
            cross_attentions=outputs.cross_attentions,
        )


class FlaxBartModule(nn.Module):
    config: BartConfig
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(
        self,
    ):
        _, vocab_size = self.config.pad_token_id, self.config.vocab_size
        embed_dim = self.config.d_model

        self.shared = nn.Embed(
            vocab_size,
            embed_dim,
            embedding_init=jax.nn.initializers.normal(self.config.init_std, self.dtype),
            dtype=self.dtype,
        )

        self.encoder = FlaxBartEncoder(self.config, dtype=self.dtype, embed_tokens=self.shared)
        self.decoder = FlaxBartDecoder(self.config, dtype=self.dtype, embed_tokens=self.shared)

    def get_encoder(self):
        return self.encoder

    @add_start_docstrings_to_model_forward(BART_INPUTS_DOCSTRING)
    def __call__(
        self,
        input_ids: Optional[jnp.ndarray] = None,
        attention_mask: Optional[jnp.ndarray] = None,
        decoder_input_ids: Optional[jnp.ndarray] = None,
        decoder_attention_mask: Optional[jnp.ndarray] = None,
        position_ids: Optional[jnp.ndarray] = None,
        decoder_position_ids: Optional[jnp.ndarray] = None,
        encoder_outputs: Optional[FlaxBaseModelOutput] = None,
        use_cache: Optional[bool] = None,
        init_cache: bool = False,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: bool = True,
        deterministic: bool = True,
    ):

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if encoder_outputs is None:
            encoder_outputs = self.encoder(
                input_ids=input_ids,
                attention_mask=attention_mask,
                position_ids=position_ids,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
                deterministic=deterministic,
            )

        elif return_dict and not isinstance(encoder_outputs, FlaxBaseModelOutput):
            encoder_outputs = FlaxBaseModelOutput(
                last_hidden_state=encoder_outputs[0],
                hidden_states=encoder_outputs[1] if len(encoder_outputs) > 1 else None,
                attentions=encoder_outputs[2] if len(encoder_outputs) > 2 else None,
            )

        decoder_outputs = self.decoder(
            input_ids=decoder_input_ids,
            attention_mask=decoder_attention_mask,
            position_ids=decoder_position_ids,
            encoder_hidden_states=encoder_outputs[0],
            encoder_attention_mask=attention_mask,
            use_cache=use_cache,
            init_cache=init_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            deterministic=deterministic,
        )

        if not return_dict:
            return decoder_outputs + encoder_outputs

        return FlaxSeq2SeqModelOutput(
            last_hidden_state=decoder_outputs.last_hidden_state,
            decoder_hidden_states=decoder_outputs.hidden_states,
            decoder_attentions=decoder_outputs.attentions,
            cross_attentions=decoder_outputs.cross_attentions,
            encoder_last_hidden_state=encoder_outputs.last_hidden_state,
            encoder_hidden_states=encoder_outputs.hidden_states,
            encoder_attentions=encoder_outputs.attentions,
        )


@add_start_docstrings(
    "The bare Bart Model transformer outputting raw hidden-states without any specific head on top.",
    BART_START_DOCSTRING,
)
class FlaxBartModel(FlaxBartPretrainedModel):
    config: BartConfig
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation
    module_class = FlaxBartModule


class FlaxBartForConditionalGenerationModule(nn.Module):
    config: BartConfig
    dtype: jnp.dtype = jnp.float32
    bias_init: Callable[..., jnp.ndarray] = jax.nn.initializers.zeros

    def setup(self):
        self.model = FlaxBartModule(config=self.config, dtype=self.dtype)
        self.lm_head = nn.Dense(self.model.shared.num_embeddings, use_bias=False, dtype=self.dtype)
        self.final_logits_bias = self.param("final_logits_bias", self.bias_init, (1, self.model.shared.num_embeddings))

    def get_encoder(self):
        return self.model.encoder

    # TODO (PS): better name ?
    def decode(
        self,
        decoder_input_ids: Optional[jnp.ndarray] = None,
        decoder_attention_mask: Optional[jnp.ndarray] = None,
        decoder_position_ids: Optional[jnp.ndarray] = None,
        encoder_hidden_states: Optional[jnp.ndarray] = None,
        encoder_attention_mask: Optional[jnp.ndarray] = None,
        init_cache: bool = False,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        deterministic: bool = True,
    ):
        outputs = self.model.decoder(
            input_ids=decoder_input_ids,
            attention_mask=decoder_attention_mask,
            position_ids=decoder_position_ids,
            encoder_hidden_states=encoder_hidden_states,
            encoder_attention_mask=encoder_attention_mask,
            init_cache=init_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            deterministic=deterministic,
        )

        hidden_states = outputs[0]

        if self.config.tie_word_embeddings:
            shared_embedding = self.model.variables["params"]["shared"]["embedding"]
            lm_logits = self.lm_head.apply({"params": {"kernel": shared_embedding.T}}, hidden_states)
        else:
            lm_logits = self.lm_head(hidden_states)

        lm_logits += self.final_logits_bias[0]

        # TODO (PS): handle return_dict == False
        # TODO (PS): what should be the output type here ?
        return FlaxSeq2SeqLMOutput(
            logits=lm_logits,
            decoder_hidden_states=outputs.hidden_states,
            decoder_attentions=outputs.attentions,
            cross_attentions=outputs.cross_attentions,
        )

    @add_start_docstrings_to_model_forward(BART_INPUTS_DOCSTRING)
    def __call__(
        self,
        input_ids: jnp.ndarray,
        attention_mask: Optional[jnp.ndarray] = None,
        decoder_input_ids: Optional[jnp.ndarray] = None,
        decoder_attention_mask: Optional[jnp.ndarray] = None,
        position_ids: Optional[jnp.ndarray] = None,
        decoder_position_ids: Optional[jnp.ndarray] = None,
        encoder_outputs: Optional[FlaxBaseModelOutput] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: bool = True,
        deterministic: bool = True,
    ):
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            position_ids=position_ids,
            decoder_position_ids=decoder_position_ids,
            encoder_outputs=encoder_outputs,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            deterministic=deterministic,
        )

        hidden_states = outputs[0]

        if self.config.tie_word_embeddings:
            shared_embedding = self.model.variables["params"]["shared"]["embedding"]
            lm_logits = self.lm_head.apply({"params": {"kernel": shared_embedding.T}}, hidden_states)
        else:
            lm_logits = self.lm_head(hidden_states)

        lm_logits += self.final_logits_bias[0]

        if not return_dict:
            output = (lm_logits,) + outputs[1:]
            return output

        return FlaxSeq2SeqLMOutput(
            logits=lm_logits,
            decoder_hidden_states=outputs.decoder_hidden_states,
            decoder_attentions=outputs.decoder_attentions,
            cross_attentions=outputs.cross_attentions,
            encoder_last_hidden_state=outputs.encoder_last_hidden_state,
            encoder_hidden_states=outputs.encoder_hidden_states,
            encoder_attentions=outputs.encoder_attentions,
        )


@add_start_docstrings(
    "The BART Model with a language modeling head. Can be used for summarization.", BART_START_DOCSTRING
)
class FlaxBartForConditionalGeneration(FlaxBartPretrainedModel):
    module_class = FlaxBartForConditionalGenerationModule
    dtype: jnp.dtype = jnp.float32

    # TODO (PS): better name ?
    def decoder(
        self,
        decoder_input_ids,
        encoder_outputs,
        encoder_attention_mask: Optional[jnp.ndarray] = None,  # TODO (PS): attention_mask ?
        decoder_attention_mask: Optional[jnp.ndarray] = None,
        decoder_position_ids: Optional[jnp.ndarray] = None,  # TODO (PS): position_ids ?
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        deterministic: bool = True,
        params: dict = None,
        past_key_values: dict = None,
        dropout_rng: PRNGKey = None,
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.return_dict

        encoder_hidden_states = encoder_outputs[0]
        if encoder_attention_mask is None:
            batch_size, sequence_length = encoder_hidden_states.shape[:2]
            encoder_attention_mask = jnp.ones((batch_size, sequence_length))

        batch_size, sequence_length = decoder_input_ids.shape
        if decoder_attention_mask is None:
            decoder_attention_mask = jnp.ones((batch_size, sequence_length))

        if decoder_position_ids is None:
            if past_key_values is not None:
                raise ValueError("Make sure to provide `decoder_position_ids` when passing `past_key_values`.")

            decoder_position_ids = jnp.broadcast_to(
                jnp.arange(sequence_length)[None, :], (batch_size, sequence_length)
            )

        # Handle any PRNG if needed
        rngs = {}
        if dropout_rng is not None:
            rngs["dropout"] = dropout_rng

        inputs = {"params": params or self.params}

        # if past_key_values are passed then cache is already initialized a private flag init_cache has to be
        # passed down to ensure cache is used. It has to be made sure that cache is marked as mutable so that
        # it can be changed by FlaxGPT2Attention module
        if past_key_values:
            inputs["cache"] = past_key_values
            mutable = ["cache"]
        else:
            mutable = False

        outputs = self.module.apply(
            inputs,
            decoder_input_ids=jnp.array(decoder_input_ids, dtype="i4"),
            decoder_attention_mask=jnp.array(decoder_attention_mask, dtype="i4"),
            decoder_position_ids=jnp.array(decoder_position_ids, dtype="i4"),
            encoder_hidden_states=encoder_outputs[0],
            encoder_attention_mask=jnp.array(encoder_attention_mask, dtype="i4"),
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            deterministic=deterministic,
            rngs=rngs,
            mutable=mutable,
            method=self.module.decode,
        )

        # add updated cache to model output
        if past_key_values is not None and return_dict:
            outputs, past_key_values = outputs
            outputs["past_key_values"] = unfreeze(past_key_values["cache"])
            return outputs
        elif past_key_values is not None and not return_dict:
            outputs, past_key_values = outputs
            outputs = outputs[:1] + (unfreeze(past_key_values["cache"]),) + outputs[1:]

        return outputs

    def prepare_inputs_for_generation(
        self,
        decoder_input_ids,
        max_length,
        attention_mask: Optional[jnp.DeviceArray] = None,
        decoder_attention_mask: Optional[jnp.DeviceArray] = None,
        encoder_outputs=None,
        **kwargs
    ):
        # initializing the cache
        batch_size, seq_length = decoder_input_ids.shape

        past_key_values = self.init_cache(batch_size, max_length, encoder_outputs)
        # Note that usually one would have to put 0's in the attention_mask for x > input_ids.shape[-1] and x < cache_length.
        # But since GPT2 uses a causal mask, those positions are masked anyways.
        # Thus we can create a single static attention_mask here, which is more efficient for compilation
        extended_attention_mask = jnp.ones((batch_size, max_length), dtype="i4")
        if decoder_attention_mask is not None:
            position_ids = decoder_attention_mask.cumsum(axis=-1) - 1
            extended_attention_mask = lax.dynamic_update_slice(extended_attention_mask, decoder_attention_mask, (0, 0))
        else:
            position_ids = jnp.broadcast_to(jnp.arange(seq_length, dtype="i4")[None, :], (batch_size, seq_length))

        return {
            "past_key_values": past_key_values,
            "encoder_outputs": encoder_outputs,
            "encoder_attention_mask": attention_mask,
            "decoder_attention_mask": extended_attention_mask,
            "decoder_position_ids": position_ids,
        }

    def update_inputs_for_generation(self, model_outputs, model_kwargs):
        model_kwargs["past_key_values"] = model_outputs.past_key_values
        model_kwargs["decoder_position_ids"] = model_kwargs["decoder_position_ids"][:, -1:] + 1
        return model_kwargs


class FlaxBartForSequenceClassificationModule(nn.Module):
    config: BartConfig
    dtype: jnp.dtype = jnp.float32
    num_labels: Optional[int] = None

    def setup(self):
        self.model = FlaxBartModule(config=self.config, dtype=self.dtype)
        self.classification_head = FlaxBartClassificationHead(
            config=self.config,
            inner_dim=self.config.d_model,
            num_classes=self.num_labels if self.num_labels is not None else self.config.num_labels,
            pooler_dropout=self.config.classifier_dropout,
        )

    @add_start_docstrings_to_model_forward(BART_INPUTS_DOCSTRING)
    def __call__(
        self,
        input_ids: jnp.ndarray,
        attention_mask: Optional[jnp.ndarray] = None,
        decoder_input_ids: Optional[jnp.ndarray] = None,
        decoder_attention_mask: Optional[jnp.ndarray] = None,
        encoder_outputs: Optional[jnp.ndarray] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: bool = True,
        deterministic: bool = True,
    ):
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            encoder_outputs=encoder_outputs,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            deterministic=deterministic,
        )

        hidden_states = outputs[0]  # last hidden state

        eos_mask = jnp.where(input_ids == self.config.eos_token_id, 1, 0)

        # The first condition is necessary to overcome jax._src.errors.ConcretizationTypeError during initialization
        if type(eos_mask) != jax.interpreters.partial_eval.DynamicJaxprTracer:
            if len(jnp.unique(eos_mask.sum(1))) > 1:
                raise ValueError("All examples must have the same number of <eos> tokens.")

            if any(eos_mask.sum(1) == 0):
                raise ValueError("There are missing <eos> tokens in input_ids")

            # Ensure to keep 1 only for the last <eos> token for each example
            eos_mask_noised = eos_mask + jnp.arange(eos_mask.shape[1]) * 1e-6
            eos_mask = jnp.where(eos_mask_noised == eos_mask_noised.max(1).reshape(-1, 1), 1, 0)

        sentence_representation = jnp.einsum("ijk, ij -> ijk", hidden_states, eos_mask).sum(1)
        logits = self.classification_head(sentence_representation, deterministic=deterministic)

        if not return_dict:
            output = (logits,) + outputs[1:]
            return output

        return FlaxSeq2SeqSequenceClassifierOutput(
            logits=logits,
            decoder_hidden_states=outputs.decoder_hidden_states,
            decoder_attentions=outputs.decoder_attentions,
            cross_attentions=outputs.cross_attentions,
            encoder_last_hidden_state=outputs.encoder_last_hidden_state,
            encoder_hidden_states=outputs.encoder_hidden_states,
            encoder_attentions=outputs.encoder_attentions,
        )


@add_start_docstrings(
    """
    Bart model with a sequence classification/head on top (a linear layer on top of the pooled output) e.g. for GLUE
    tasks.
    """,
    BART_START_DOCSTRING,
)
class FlaxBartForSequenceClassification(FlaxBartPretrainedModel):
    module_class = FlaxBartForSequenceClassificationModule
    dtype = jnp.float32


class FlaxBartForQuestionAnsweringModule(nn.Module):
    config: BartConfig
    dtype: jnp.dtype = jnp.float32
    num_labels = 2

    def setup(self):
        self.model = FlaxBartModule(config=self.config, dtype=self.dtype)
        self.qa_outputs = nn.Dense(self.num_labels, dtype=self.dtype)

    @add_start_docstrings_to_model_forward(BART_INPUTS_DOCSTRING)
    def __call__(
        self,
        input_ids: jnp.ndarray,
        attention_mask: Optional[jnp.ndarray] = None,
        decoder_input_ids: Optional[jnp.ndarray] = None,
        decoder_attention_mask: Optional[jnp.ndarray] = None,
        encoder_outputs: Optional[jnp.ndarray] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: bool = True,
        deterministic: bool = True,
    ):
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            encoder_outputs=encoder_outputs,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            deterministic=deterministic,
        )

        sequence_output = outputs[0]

        logits = self.qa_outputs(sequence_output)
        start_logits, end_logits = jnp.split(logits, logits.shape[-1], axis=-1)
        start_logits = start_logits.squeeze(-1)
        end_logits = end_logits.squeeze(-1)

        if not return_dict:
            output = (
                start_logits,
                end_logits,
            ) + outputs[1:]
            return output

        return FlaxSeq2SeqQuestionAnsweringModelOutput(
            start_logits=start_logits,
            end_logits=end_logits,
            decoder_hidden_states=outputs.decoder_hidden_states,
            decoder_attentions=outputs.decoder_attentions,
            cross_attentions=outputs.cross_attentions,
            encoder_last_hidden_state=outputs.encoder_last_hidden_state,
            encoder_hidden_states=outputs.encoder_hidden_states,
            encoder_attentions=outputs.encoder_attentions,
        )


@add_start_docstrings(
    """
    BART Model with a span classification head on top for extractive question-answering tasks like SQuAD (a linear
    layer on top of the hidden-states output to compute `span start logits` and `span end logits`).
    """,
    BART_START_DOCSTRING,
)
class FlaxBartForQuestionAnswering(FlaxBartPretrainedModel):
    module_class = FlaxBartForQuestionAnsweringModule
    dtype = jnp.float32