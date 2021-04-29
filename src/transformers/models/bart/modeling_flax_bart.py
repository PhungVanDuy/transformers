# coding=utf-8
# Copyright 2021 The Fairseq Authors, The HuggingFace Inc. team And Daniel Stancl. All rights reserved.
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
from typing import Callable, Optional, Tuple

import numpy as np

import flax.linen as nn
import jax
import jax.numpy as jnp
from flax.core.frozen_dict import FrozenDict
from flax.linen import dot_product_attention
from jax import lax
from jax.random import PRNGKey

from ...file_utils import add_start_docstrings, add_start_docstrings_to_model_forward
from ...modeling_flax_utils import ACT2FN, FlaxPreTrainedModel, overwrite_call_docstring
from ...utils import logging
from .configuration_bart import BartConfig


logger = logging.get_logger(__name__)

_CONFIG_FOR_DOC = "BartConfig"
_TOKENIZER_FOR_DOC = "BartTokenizer"


def shift_tokens_right(input_ids: jnp.ndarray, pad_token_id: int, decoder_start_token_id: int) -> jnp.ndarray:
    """
    Shift input ids one token to the right.
    """
    shifted_input_ids = jnp.roll(input_ids, 1, axis=-1)
    shifted_input_ids = jax.ops.index_update(shifted_input_ids, (..., 0), decoder_start_token_id)
    # replace possible -100 values in labels by `pad_token_id`
    shifted_input_ids = jax.ops.index_update(shifted_input_ids, shifted_input_ids == -100, pad_token_id)

    return shifted_input_ids


def _make_causal_mask(
    input_ids_shape: Tuple[int, int], dtype: jnp.dtype, past_key_values_length: int = 0
) -> jnp.ndarray:
    """
    Make causal mask used for bi-directional self-attention.
    """
    bsz, tgt_len = input_ids_shape
    mask = jnp.full((tgt_len, tgt_len), float("-inf"))
    mask_cond = jnp.arange(mask.shape[-1])
    mask = jax.ops.index_update(mask, mask_cond < (mask_cond + 1).reshape(mask.shape[-1], 1), 0)
    mask = mask.astype(dtype)

    if past_key_values_length > 0:
        mask = jnp.concatenate([jnp.zeros((tgt_len, past_key_values_length), dtype=dtype), mask], axis=-1)
    return jnp.tile(mask[None, None, :, :], (bsz, 1, 1, 1))


def _expand_mask(mask: jnp.ndarray, dtype: jnp.dtype, tgt_len: Optional[int] = None) -> jnp.ndarray:
    """
    Expands attention_mask from `[bsz, seq_len]` to `[bsz, 1, tgt_seq_len, src_seq_len]`.
    """
    bsz, src_len = mask.shape
    tgt_len = tgt_len if tgt_len is not None else src_len

    expanded_mask = jnp.tile(mask[:, None, None, :], (1, 1, tgt_len, 1)).astype(dtype)
    inverted_mask = 1.0 - expanded_mask

    return inverted_mask * jnp.finfo(dtype).min


class FlaxBartLearnedPositionalEmbedding(nn.Module):
    """
    This module learns positional embeddings up to a fixed maximum size.
    """

    num_embeddings: int
    embedding_dim: int
    config: BartConfig
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self) -> None:
        # Bart is set up so that if padding_idx is specified then offset the embedding ids by 2
        # and adjust num_embeddings appropriately. Other models don't have this hack
        self.offset = 2

        self.position_embeddings = nn.Embed(
            self.num_embeddings + self.offset,
            self.embedding_dim,
            embedding_init=jax.nn.initializers.normal(self.config.init_std, self.dtype),
            dtype=self.dtype,
        )

    def __call__(self, input_ids_shape: Tuple[int], past_key_values_length: int = 0) -> jnp.ndarray:
        bsz, seq_len = input_ids_shape[:2]
        positions = jnp.arange(past_key_values_length, past_key_values_length + seq_len, dtype=jnp.uint32)
        return self.position_embeddings(positions + self.offset)


class FlaxBartAttention(nn.Module):
    config: BartConfig
    embed_dim: int
    num_heads: int
    dropout: float = 0.0
    is_decoder: bool = False
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

    def _shape(self, x, seq_len: int, bsz: int):
        return x.reshape(bsz, seq_len, self.num_heads, self.head_dim).transpose((0, 2, 1, 3))

    def __call__(
        self,
        hidden_states: jnp.ndarray,
        key_value_states: Optional[jnp.ndarray] = None,
        past_key_value: Optional[Tuple[jnp.ndarray]] = None,
        attention_mask: Optional[jnp.ndarray] = None,
        layer_head_mask: Optional[jnp.ndarray] = None,
        output_attentions: bool = False,
        deterministic: bool = True,
    ) -> Tuple[jnp.ndarray]:
        """Input shape: Batch x Time x Channel"""

        # if key_value_states are provided this layer is used as a cross-attention layer
        # for the decoder
        is_cross_attention = key_value_states is not None
        bsz, tgt_len, embed_dim = hidden_states.shape

        # get query proj
        query_states = self.q_proj(hidden_states)
        # get key, value proj
        if is_cross_attention and past_key_value is not None:
            # reuse k,v, cross_attentions
            key_states = past_key_value[0]
            value_states = past_key_value[1]
        elif is_cross_attention:
            # cross_attentions
            key_states = self._shape(self.k_proj(key_value_states), -1, bsz)
            value_states = self._shape(self.v_proj(key_value_states), -1, bsz)
        elif past_key_value is not None:
            # reuse k, v, self_attention
            key_states = self._shape(self.k_proj(hidden_states), -1, bsz)
            value_states = self._shape(self.v_proj(hidden_states), -1, bsz)
            key_states = jnp.concatenate([past_key_value[0], key_states], dim=2)
            value_states = jnp.concatenate([past_key_value[1], value_states], dim=2)
        else:
            # self_attention
            key_states = self._shape(self.k_proj(hidden_states), -1, bsz)
            value_states = self._shape(self.v_proj(hidden_states), -1, bsz)

        if self.is_decoder:
            # if cross_attention save Tuple(jnp.ndarray, jnp.ndarray) of all cross attention key/value_states.
            # Further calls to cross_attention layer can then reuse all cross-attention
            # key/value_states (first "if" case)
            # if uni-directional self-attention (decoder) save Tuple(jnp.ndarray, jnp.ndarray) of
            # all previous decoder key/value_states. Further calls to uni-directional self-attention
            # can concat previous decoder key/value_states to current projected key/value_states (third "elif" case)
            # if encoder bi-directional self-attention `past_key_value` is always `None`
            past_key_value = (key_states, value_states)

        proj_shape = (bsz * self.num_heads, -1, self.head_dim)
        query_states = self._shape(query_states, tgt_len, bsz).reshape(proj_shape)
        key_states = key_states.reshape(proj_shape)
        value_states = value_states.reshape(proj_shape)

        src_len = key_states.shape[1]
        attn_weights = jnp.matmul(query_states, key_states.transpose((0, 2, 1)))

        assert attn_weights.shape == (
            bsz * self.num_heads,
            tgt_len,
            src_len,
        ), f"Attention weights should be of size {(bsz * self.num_heads, tgt_len, src_len)}, but is {attn_weights.shape}"

        if attention_mask is not None:
            assert attention_mask.shape == (
                bsz,
                1,
                tgt_len,
                src_len,
            ), f"Attention mask should be of size {(bsz, 1, tgt_len, src_len)}, but is {attention_mask.shape}"
            attn_weights = attn_weights.reshape(bsz, self.num_heads, tgt_len, src_len) + attention_mask
            attn_weights = attn_weights.reshape(bsz * self.num_heads, tgt_len, src_len)

        attn_weights = jax.nn.softmax(attn_weights, axis=-1)

        if layer_head_mask is not None:
            assert layer_head_mask.shape == (
                self.num_heads,
            ), f"Head mask for a single layer should be of size {(self.num_heads,)}, but is {layer_head_mask.shape}"
            attn_weights = layer_head_mask.reshape(1, -1, 1, 1) * attn_weights.reshape(
                bsz, self.num_heads, tgt_len, src_len
            )
            attn_weights = attn_weights.reshape(bsz * self.num_heads, tgt_len, src_len)

        if output_attentions:
            # this operation is a bit awkward, but it's required to
            # make sure that attn_weights keeps its gradient.
            # In order to do so, attn_weights have to be reshaped
            # twice and have to be reused in the following
            attn_weights_reshaped = attn_weights.reshape(bsz, self.num_heads, tgt_len, src_len)
            attn_weights = attn_weights_reshaped.reshape(bsz * self.num_heads, tgt_len, src_len)
        else:
            attn_weights_reshaped = None

        attn_probs = self.dropout_layer(attn_weights, deterministic=deterministic)

        attn_output = jnp.matmul(attn_probs, value_states)

        assert attn_output.shape == (
            bsz * self.num_heads,
            tgt_len,
            self.head_dim,
        ), f"`attn_output` should be of size {(bsz, self.num_heads, tgt_len, self.head_dim)}, but is {attn_output.shape}"

        attn_output = (
            attn_output.reshape(bsz, self.num_heads, tgt_len, self.head_dim)
            .transpose((0, 2, 1, 3))
            .reshape(bsz, tgt_len, embed_dim)
        )

        attn_output = self.out_proj(attn_output)

        return attn_output, attn_weights_reshaped, past_key_value


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
        layer_head_mask: jnp.ndarray,
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
        hidden_states, attn_weights, _ = self.self_attn(
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            layer_head_mask=layer_head_mask,
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
        layer_head_mask: Optional[jnp.ndarray] = None,
        cross_attn_layer_head_mask: Optional[jnp.ndarray] = None,
        past_key_value: Optional[Tuple[jnp.ndarray]] = None,
        output_attentions: bool = True,
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
        # decoder uni-directional self-attention cached key/values tuple is at positions 1,2
        self_attn_past_key_value = past_key_value[:2] if past_key_value is not None else None
        # add present self-attn cache to positions 1,2 of present_key_value tuple
        hidden_states, self_attn_weights, present_key_value = self.self_attn(
            hidden_states=hidden_states,
            past_key_value=self_attn_past_key_value,
            attention_mask=attention_mask,
            layer_head_mask=layer_head_mask,
            output_attentions=output_attentions,
        )
        hidden_states = self.dropout_layer(hidden_states, deterministic=deterministic)
        hidden_states = residual + hidden_states
        hidden_states = self.self_attn_layer_norm(hidden_states)

        # Cross-Attention Block
        cross_attn_present_key_value = None
        cross_attn_weights = None
        if encoder_hidden_states is not None:
            residual = hidden_states

            # cross_attn cached key/values tuple is at positions 3,4 of present_key_value tuple
            cross_attn_past_key_value = past_key_value[-2:] if past_key_value is not None else None
            hidden_states, cross_attn_weights, cross_attn_present_key_value = self.encoder_attn(
                hidden_states=hidden_states,
                key_value_states=encoder_hidden_states,
                attention_mask=encoder_attention_mask,
                layer_head_mask=cross_attn_layer_head_mask,
                past_key_value=cross_attn_past_key_value,
                output_attentions=output_attentions,
            )
            hidden_states = self.dropout_layer(hidden_states, deterministic=deterministic)
            hidden_states = residual + hidden_states
            hidden_states = self.encoder_attn_layer_norm(hidden_states)

            # add cross-attn to positions 3,4 of present_key_value tuple
            present_key_value = present_key_value + cross_attn_present_key_value

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


class FlaxBartPretrainedModel(FlaxPreTrainedModel):
    config_class = BartConfig
    base_model_prefix = "bart"
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
        attention_mask = jnp.ones_like(input_ids)
        decoder_input_ids = jnp.zeros(input_shape, dtype="i4")
        decoder_attention_mask = jnp.ones_like(input_ids)

        params_rng, dropout_rng = jax.random.split(rng)
        rngs = {"params": params_rng, "dropout": dropout_rng}

        return self.module.init(rngs, input_ids, attention_mask, decoder_input_ids, decoder_attention_mask)["params"]

    def __call__(
        self,
        input_ids: Optional[jnp.ndarray] = None,
        attention_mask: Optional[jnp.ndarray] = None,
        decoder_input_ids: Optional[jnp.ndarray] = None,
        decoder_attention_mask: Optional[jnp.ndarray] = None,
        head_mask: Optional[jnp.ndarray] = None,
        decoder_head_mask: Optional[jnp.ndarray] = None,
        cross_attn_head_mask: Optional[jnp.ndarray] = None,
        encoder_outputs: Optional[jnp.ndarray] = None,
        past_key_values: Optional[jnp.ndarray] = None,
        inputs_embeds: Optional[jnp.ndarray] = None,
        decoder_inputs_embeds: Optional[jnp.ndarray] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = False,
        deterministic: bool = True,
        params: dict = None,
        dropout_rng: PRNGKey = None,
    ):
        # Sanity check
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            input_shape = input_ids.shape
            input_ids = input_ids.reshape(-1, input_shape[-1])
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.shape[:-1]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        if attention_mask is None:
            attention_mask = jnp.ones(input_shape)
        if decoder_input_ids is None:
            decoder_input_ids = shift_tokens_right(
                input_ids, self.config.pad_token_id, decoder_start_token_id=self.config.decoder_start_token_id
            )
        if decoder_attention_mask is None:
            decoder_attention_mask = attention_mask

        # Handle any PRNG if needed
        rngs = {}
        if dropout_rng is not None:
            rngs["dropout"] = dropout_rng

        return self.module.apply(
            {"params": params or self.params},
            input_ids=jnp.array(input_ids, dtype="i4") if input_ids is not None else None,
            attention_mask=jnp.array(attention_mask, dtype="i4") if attention_mask is not None else None,
            decoder_input_ids=jnp.array(decoder_input_ids, dtype="i4") if decoder_input_ids is not None else None,
            head_mask=jnp.array(head_mask, dtype="i4") if head_mask is not None else None,
            decoder_head_mask=jnp.array(decoder_head_mask, dtype="i4") if decoder_head_mask is not None else None,
            cross_attn_head_mask=jnp.array(cross_attn_head_mask, dtype="i4")
            if cross_attn_head_mask is not None
            else None,
            encoder_outputs=jnp.array(encoder_outputs, dtype="fp32") if encoder_outputs is not None else None,
            past_key_values=jnp.array(past_key_values, dtype="fp32") if past_key_values is not None else None,
            inputs_embeds=jnp.array(inputs_embeds, dtype="fp32") if inputs_embeds is not None else None,
            decoder_inputs_embeds=jnp.array(decoder_inputs_embeds, dtype="fp32")
            if decoder_inputs_embeds is not None
            else None,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            deterministic=deterministic,
            rngs=rngs,
        )


BART_START_DOCSTRING = r"""
    This model inherits from :class:`~transformers.FlaxPreTrainedModel`. Check the superclass documentation for the
    generic methods the library implements for all its model (such as downloading, saving and converting weights from
    PyTorch models)
    This model is also a Flax Linen `flax.nn.Module
    <https://flax.readthedocs.io/en/latest/_autosummary/flax.nn.module.html>`__ subclass. Use it as a regular Flax
    Module and refer to the Flax documentation for all matter related to general usage and behavior.
    Finally, this model supports inherent JAX features such as:
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

    def setup(self, embed_tokens: Optional[nn.Embed] = None):
        self.dropout_layer = nn.Dropout(rate=self.config.dropout)
        self.layerdrop = self.config.encoder_layerdrop
        self.layerdrop_layer = nn.Dropout(rate=self.config.encoder_layerdrop)

        embed_dim = self.config.d_model
        self.padding_idx = self.config.pad_token_id
        self.max_source_positions = self.config.max_position_embeddings
        self.embed_scale = math.sqrt(embed_dim) if self.config.scale_embedding else 1.0

        if embed_tokens is not None:
            self.embed_tokens = embed_tokens
        else:
            self.embed_tokens = nn.Embed(
                self.config.vocab_size,
                embed_dim,
                embedding_init=jax.nn.initializers.normal(self.config.init_std, self.dtype),
                dtype=self.dtype,
            )  # TODO: solve missing self.padding_idx

        self.embed_positions = FlaxBartLearnedPositionalEmbedding(
            self.config.max_position_embeddings,
            embed_dim,
            self.config,
        )
        self.layers = [
            FlaxBartEncoderLayer(self.config, dtype=self.dtype) for _ in range(self.config.num_hidden_layers)
        ]
        self.layernorm_embedding = nn.LayerNorm(dtype=self.dtype)

    def __call__(
        self,
        input_ids: Optional[jnp.ndarray] = None,
        attention_mask: Optional[jnp.ndarray] = None,
        head_mask: Optional[jnp.ndarray] = None,
        inputs_embeds: Optional[jnp.ndarray] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = False,
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

        # retrieve input_ids and inputs_embeds
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            input_shape = input_ids.shape
            input_ids = input_ids.reshape(-1, input_shape[-1])
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.shape[:-1]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids) * self.embed_scale

        embed_pos = self.embed_positions(input_shape)

        hidden_states = inputs_embeds + embed_pos
        hidden_states = self.layernorm_embedding(hidden_states)
        hidden_states = self.dropout_layer(hidden_states, deterministic=deterministic)

        # expand attention_mask
        if attention_mask is not None:
            # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
            attention_mask = _expand_mask(attention_mask, inputs_embeds.dtype)

        encoder_states = () if output_hidden_states else None
        all_attentions = () if output_attentions else None

        # check if head_mask has a correct number of layers specified if desired
        if head_mask is not None:
            assert head_mask.shape[0] == (
                len(self.layers)
            ), f"The head_mask should be specified for {len(self.layers)} layers, but it is for {head_mask.shape[0]}."
        for idx, encoder_layer in enumerate(self.layers):
            if output_hidden_states:
                encoder_states = encoder_states + (hidden_states,)
            # add LayerDrop (see https://arxiv.org/abs/1909.11556 for description)
            dropout_probability = random.uniform(0, 1)
            if deterministic and (dropout_probability < self.layerdrop):  # skip the layer
                hidden_states, attn = (None, None)

            hidden_states, attn = encoder_layer(
                hidden_states,
                attention_mask,
                head_mask[idx] if head_mask is not None else None,
                True,  # we want to always output attentions at this step (at least so far for debugging purposes :) )
                deterministic,
            )

            if output_attentions:
                all_attentions = all_attentions + (attn,)

            if output_hidden_states:
                encoder_states = encoder_states + (hidden_states,)

        if not return_dict:
            return tuple(v for v in [hidden_states, encoder_states, all_attentions] if v is not None)
        raise NotImplementedError(
            "TODO: Return with return_dict is not implemented yet! Please use `return_dict=False`"
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

    def setup(self, embed_tokens: Optional[nn.Embed] = None):
        self.dropout_layer = nn.Dropout(rate=self.config.dropout)
        self.layerdrop = self.config.decoder_layerdrop
        self.layerdrop_layer = nn.Dropout(rate=self.layerdrop)

        embed_dim = self.config.d_model
        self.padding_idx = self.config.pad_token_id
        self.max_target_positions = self.config.max_position_embeddings
        self.embed_scale = math.sqrt(self.config.d_model) if self.config.scale_embedding else 1.0

        if embed_tokens is not None:
            self.embed_tokens = embed_tokens
        else:
            self.embed_tokens = nn.Embed(
                self.config.vocab_size,
                embed_dim,
                embedding_init=jax.nn.initializers.normal(self.config.init_std, self.dtype),
                dtype=self.dtype,
            )  # TODO: solve missing self.padding_idx

        self.embed_positions = FlaxBartLearnedPositionalEmbedding(
            self.config.max_position_embeddings,
            embed_dim,
            self.config,
        )
        self.layers = [FlaxBartDecoderLayer(self.config, dtype=self.dtype) for _ in range(self.config.decoder_layers)]
        self.layernorm_embedding = nn.LayerNorm(dtype=self.dtype)

    def get_input_embeddings(self):
        return self.embed_tokens

    def set_input_embeddings(self, value):
        self.embed_tokens = value

    def _prepare_decoder_attention_mask(self, attention_mask, input_shape, inputs_embeds, past_key_values_length):
        # create causal mask
        # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
        combined_attention_mask = None
        if input_shape[-1] > 1:
            combined_attention_mask = _make_causal_mask(
                input_shape, inputs_embeds.dtype, past_key_values_length=past_key_values_length
            )

        if attention_mask is not None:
            # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
            expanded_attn_mask = _expand_mask(attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1])
            combined_attention_mask = (
                expanded_attn_mask if combined_attention_mask is None else expanded_attn_mask + combined_attention_mask
            )
        return combined_attention_mask

    def __call__(
        self,
        input_ids: Optional[jnp.ndarray] = None,
        attention_mask: Optional[jnp.ndarray] = None,
        encoder_hidden_states: Optional[jnp.ndarray] = None,
        encoder_attention_mask: Optional[jnp.ndarray] = None,
        head_mask: Optional[jnp.ndarray] = None,
        cross_attn_head_mask: Optional[jnp.ndarray] = None,
        past_key_values: Optional[jnp.ndarray] = None,
        inputs_embeds: Optional[jnp.ndarray] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = False,
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
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # retrieve input_ids and inputs_embeds
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            input_shape = input_ids.shape
            input_ids = input_ids.reshape(-1, input_shape[-1])
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.shape[:-1]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        # past_key_values_length
        past_key_values_length = past_key_values[0][0].shape[2] if past_key_values is not None else 0

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids) * self.embed_scale

        attention_mask = self._prepare_decoder_attention_mask(
            attention_mask, input_shape, inputs_embeds, past_key_values_length
        )

        # expand encoder attention mask
        if encoder_hidden_states is not None and encoder_attention_mask is not None:
            # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
            encoder_attention_mask = _expand_mask(encoder_attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1])

        # embed positions
        positions = self.embed_positions(input_shape, past_key_values_length)

        hidden_states = inputs_embeds + positions
        hidden_states = self.layernorm_embedding(hidden_states)

        hidden_states = self.dropout_layer(hidden_states, deterministic=deterministic)

        # decoder layers
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None
        all_cross_attentions = () if (output_attentions and encoder_hidden_states is not None) else None

        # check if head_mask/cross_attn_head_mask has a correct number of layers specified if desired
        for attn_mask, mask_name in zip([head_mask, cross_attn_head_mask], ["head_mask", "cross_attn_head_mask"]):
            if attn_mask is not None:
                assert attn_mask.shape[0] == (
                    len(self.layers)
                ), f"The `{mask_name}` should be specified for {len(self.layers)} layers, but it is for {head_mask.shape[0]}."
        for idx, decoder_layer in enumerate(self.layers):
            if output_hidden_states:
                all_hidden_states += (hidden_states,)
                # add LayerDrop (see https://arxiv.org/abs/1909.11556 for description)
            dropout_probability = random.uniform(0, 1)
            if deterministic and (dropout_probability < self.layerdrop):
                continue

            past_key_value = past_key_values[idx] if past_key_values is not None else None

            layer_outputs = decoder_layer(
                hidden_states,
                attention_mask,
                encoder_hidden_states,
                encoder_attention_mask,
                head_mask[idx] if head_mask is not None else None,
                cross_attn_head_mask[idx] if cross_attn_head_mask is not None else None,
                past_key_value,
                True,  # we want to always output attentions at this step
                deterministic,
            )

            if output_attentions:
                all_self_attns += (layer_outputs[1],)

                if encoder_hidden_states is not None:
                    all_cross_attentions += (layer_outputs[2],)

        # add hidden states from the last decoder layer
        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        if not return_dict:
            return tuple(
                v for v in [hidden_states, all_hidden_states, all_self_attns, all_cross_attentions] if v is not None
            )
        raise NotImplementedError(
            "TODO: Return with return_dict is not implemented yet! Please use `return_dict=False`"
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
        )  # TODO: solve missing self.padding_idx

        self.encoder = FlaxBartEncoder(self.config, dtype=self.dtype)
        self.decoder = FlaxBartDecoder(self.config, dtype=self.dtype)

    def __call__(
        self,
        input_ids: Optional[jnp.ndarray] = None,
        attention_mask: Optional[jnp.ndarray] = None,
        decoder_input_ids: Optional[jnp.ndarray] = None,
        decoder_attention_mask: Optional[jnp.ndarray] = None,
        head_mask: Optional[jnp.ndarray] = None,
        decoder_head_mask: Optional[jnp.ndarray] = None,
        cross_attn_head_mask: Optional[jnp.ndarray] = None,
        encoder_outputs: Optional[jnp.ndarray] = None,
        past_key_values: Optional[jnp.ndarray] = None,
        inputs_embeds: Optional[jnp.ndarray] = None,
        decoder_inputs_embeds: Optional[jnp.ndarray] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = False,
        deterministic: bool = True,
    ):
        if decoder_input_ids is None and decoder_inputs_embeds is None:
            decoder_input_ids = shift_tokens_right(
                input_ids, self.config.pad_token_id, self.config.decoder_start_token_id
            )

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if encoder_outputs is None:
            encoder_outputs = self.encoder(
                input_ids=input_ids,
                attention_mask=attention_mask,
                head_mask=head_mask,
                inputs_embeds=inputs_embeds,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
                deterministic=deterministic,
            )

        decoder_outputs = self.decoder(
            input_ids=decoder_input_ids,
            attention_mask=decoder_attention_mask,
            encoder_hidden_states=encoder_outputs[0],
            encoder_attention_mask=attention_mask,
            head_mask=decoder_head_mask,
            cross_attn_head_mask=cross_attn_head_mask,
            past_key_values=past_key_values,
            inputs_embeds=decoder_inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            deterministic=deterministic,
        )

        if not return_dict:
            return decoder_outputs + encoder_outputs
        raise NotImplementedError(
            "TODO: Return with return_dict is not implemented yet! Please use `return_dict=False`"
        )


class FlaxBartModel(FlaxBartPretrainedModel):
    config: BartConfig
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation
    module_class = FlaxBartModule

    def get_input_embeddings(self):
        return self.shared

    def get_encoder(self):
        return self.encoder

    def get_decoder(self):
        return self.decoder