# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

from unittest import mock

import pytest
import torch

from vllm.config import (CacheConfig, DeviceConfig, LoadConfig, ModelConfig,
                         ParallelConfig, SchedulerConfig, SpeculativeConfig,
                         VllmConfig)
from vllm.model_executor.models.llama import LlamaForCausalLM
from vllm.platforms import current_platform
from vllm.v1.spec_decode.eagle import EagleProposer

model_dir = "meta-llama/Llama-3.1-8B-Instruct"
eagle_dir = "yuhuili/EAGLE-LLaMA3.1-Instruct-8B"
eagle3_dir = "yuhuili/EAGLE3-LLaMA3.1-Instruct-8B"


def _create_proposer(method: str, k: int) -> EagleProposer:
    model_config = ModelConfig(model=model_dir,
                               task="generate",
                               max_model_len=100,
                               tokenizer=model_dir,
                               tokenizer_mode="auto",
                               dtype="auto",
                               seed=None,
                               trust_remote_code=False)

    # Choose model directory based on method
    draft_model_dir = eagle_dir if method == "eagle" else eagle3_dir

    speculative_config = SpeculativeConfig(
        target_model_config=model_config,
        target_parallel_config=ParallelConfig(),
        model=draft_model_dir,
        method=method,
        num_speculative_tokens=k,
    )

    vllm_config = VllmConfig(
        model_config=model_config,
        cache_config=CacheConfig(),
        speculative_config=speculative_config,
        device_config=DeviceConfig(device=current_platform.device_type),
        parallel_config=ParallelConfig(),
        load_config=LoadConfig(),
        scheduler_config=SchedulerConfig())

    return EagleProposer(vllm_config=vllm_config,
                         device=current_platform.device_type)


def test_prepare_inputs():
    """
    cu_target_query_lens: [0, a, a + b, a + b + c]
    num_rejected_tokens: [n1, n2, n3]
    num_tokens_per_req: [a - n1, b - n2, c - n3]
    cu_num_tokens: [0, a - n1, a + b - n1 - n2, a + b + c - n1 - n2 - n3]
    token_indices: [0, 1, ..., a - n1 - 1,
                    a, a + 1, ..., a + b - n2 - 1,
                    a + b, a + b + 1, ..., a + b + c - n3 - 1]
    """
    device = torch.device(current_platform.device_type)

    # a = 4, b = 7, c = 5
    # n1 = 1, n2 = 3, n3 = 2

    # Cumulative lengths: [0, 4, 11, 16]
    cu_target_query_lens = torch.tensor([0, 4, 11, 16],
                                        dtype=torch.int32,
                                        device=device)

    # Rejected tokens per request: [1, 3, 2]
    num_rejected_tokens = torch.tensor([1, 3, 2],
                                       dtype=torch.int32,
                                       device=device)

    # Expected calculations:
    # query_len_per_req = [4, 7, 5]
    # num_tokens_per_req = [3, 4, 3]  (after subtracting rejected tokens)
    # Expected cumulative counts: [0, 3, 7, 10]
    expected_cu_num_tokens = torch.tensor([0, 3, 7, 10],
                                          dtype=torch.int32,
                                          device=device)

    # Expected token indices (mapped from original positions):
    # First request: indices 0, 1, 2      (keeping first 3 from positions 0-3)
    # Second request: indices 4, 5, 6, 7  (keeping first 4 from positions 4-10)
    # Third request: indices 11, 12, 13   (keeping first 3 from positions 11-15)
    expected_token_indices = torch.tensor(
        [
            0,
            1,
            2,  # First request: 3 tokens (4-1)
            4,
            5,
            6,
            7,  # Second request: 4 tokens (7-3)
            11,
            12,
            13  # Third request: 3 tokens (5-2)
        ],
        dtype=torch.int32,
        device=device)

    # n1 + n2 + n3 - a - b -c
    num_tokens = cu_target_query_lens[-1].item() - num_rejected_tokens.sum(
    ).item()

    cu_num_tokens, token_indices = EagleProposer.prepare_inputs(
        cu_target_query_lens, num_rejected_tokens, num_tokens)

    assert torch.equal(cu_num_tokens, expected_cu_num_tokens)
    assert token_indices.shape[0] == expected_cu_num_tokens[-1].item()
    assert torch.equal(token_indices, expected_token_indices)


@pytest.mark.parametrize("method,proposer_helper", [
    ("eagle", lambda k: _create_proposer("eagle", k)),
    ("eagle3", lambda k: _create_proposer("eagle3", k)),
])
@pytest.mark.parametrize("pp_size", [1, 2])
@pytest.mark.parametrize("use_distinct_embed_tokens", [True, False])
@mock.patch('vllm.v1.spec_decode.eagle.get_pp_group')
@mock.patch('vllm.v1.spec_decode.eagle.get_layers_from_vllm_config')
@mock.patch('vllm.v1.spec_decode.eagle.get_model')
def test_load_model(mock_get_model, mock_get_layers, mock_get_pp_group, method,
                    proposer_helper, pp_size, use_distinct_embed_tokens):
    # Setup draft model mock
    mock_model = mock.MagicMock()
    if use_distinct_embed_tokens:
        # Some models can have a different hidden size than the target model,
        # so we test that their embed_tokens doesn't get overwritten
        mock_model.model.embed_tokens.weight.shape = (131072, 2048)
    else:
        mock_model.model.embed_tokens.weight.shape = (131072, 4096)

    mock_get_model.return_value = mock_model

    # Setup mocks for attention layers
    target_attn_layers = {
        "target_attn_1": mock.MagicMock(),
        "target_attn_2": mock.MagicMock()
    }
    # Draft model has one extra attention layer compared to target model
    all_attn_layers = {
        **target_attn_layers, "draft_extra_attn": mock.MagicMock()
    }

    # Make mock_get_layers return different values for each call
    mock_get_layers.side_effect = [target_attn_layers, all_attn_layers]

    # Setup mock for pp group to return the appropriate value for world size
    mock_pp_group = mock.MagicMock()
    mock_pp_group.world_size = pp_size
    mock_get_pp_group.return_value = mock_pp_group

    # Setup the target model mock with a custom class so that
    # isinstance() checks match the expected type.
    class _TargetModelStub(LlamaForCausalLM):
        model: mock.MagicMock
        lm_head: mock.MagicMock

    target_model = mock.create_autospec(_TargetModelStub, instance=True)
    target_model.model = mock.MagicMock()
    target_model.model.embed_tokens.weight.shape = (131072, 4096)

    from vllm.model_executor.models import SupportsMultiModal
    assert not isinstance(target_model, SupportsMultiModal)

    if method == "eagle":
        target_model.lm_head = mock.MagicMock()

    # Create proposer using the helper function
    proposer = proposer_helper(k=8)

    # Call the method under test
    proposer.load_model(target_model)

    # Verify common interactions
    mock_get_model.assert_called_once()

    # Verify that EAGLE models gain the lm head from the target model
    if method == "eagle":
        assert proposer.model.lm_head == target_model.lm_head

    # Verify that the embed tokens are set correctly
    # If pp_size is > 1, the embed tokens should be distinct
    if pp_size > 1 or use_distinct_embed_tokens:
        assert proposer.model.model.embed_tokens != \
            target_model.model.embed_tokens
    else:
        # When pp_size is 1 and the draft and target models have
        # embed_tokens of the same shape, they should be shared.
        assert proposer.model.model.embed_tokens == \
            target_model.model.embed_tokens


@pytest.mark.parametrize("num_speculative_tokens", [1, 3, 8])
def test_propose(num_speculative_tokens):
    # Use GPU device
    device = torch.device(current_platform.device_type)

    # Setup test parameters
    batch_size = 2
    seq_len_1 = 5
    seq_len_2 = 3
    total_tokens = seq_len_1 + seq_len_2
    vocab_size = 100

    # Create proposer first so we can use its actual hidden_size
    proposer = _create_proposer("eagle", num_speculative_tokens)
    # Get the hidden_size from the proposer to ensure consistency
    hidden_size = proposer.hidden_size

    # Helper to create deterministic logits that will produce specific tokens
    def create_deterministic_logits(token_ids):
        logits = torch.full((batch_size, vocab_size), -100.0, device=device)
        for i, token_id in enumerate(token_ids):
            logits[i, token_id] = 100.0
        return logits

    # We mock a model that returns deterministic logits
    # Sequence 1: 42, 43, 44, ...
    # Sequence 2: 60, 61, 62, ...
    base_token_ids = [42, 60]

    # Skip loading the model and replace it with a mock directly
    # Create the mock model with deterministic outputs
    model_mock = mock.MagicMock()

    # Setup for model forward calls
    forward_returns = []
    for i in range(num_speculative_tokens):
        if i == 0:
            # First call uses all tokens
            h_logits = torch.zeros(total_tokens, hidden_size, device=device)
            h_states = torch.zeros(total_tokens, hidden_size, device=device)
        else:
            # Subsequent calls use batch_size tokens
            h_logits = torch.zeros(batch_size, hidden_size, device=device)
            h_states = torch.zeros(batch_size, hidden_size, device=device)
        forward_returns.append((h_logits, h_states))

    # For single token case, we only need the first item;
    # for multi-token, we need the sequence
    if num_speculative_tokens == 1:
        model_mock.return_value = forward_returns[0]
    else:
        model_mock.side_effect = forward_returns

    # Setup for compute_logits calls
    logits_returns = []
    for i in range(num_speculative_tokens):
        # For each call, increment the base token IDs
        current_tokens = [base_id + i for base_id in base_token_ids]
        logits_returns.append(create_deterministic_logits(current_tokens))

    if num_speculative_tokens == 1:
        model_mock.compute_logits.return_value = logits_returns[0]
    else:
        model_mock.compute_logits.side_effect = logits_returns

    # Assign the mock to the proposer
    proposer.model = model_mock

    # Assign draft attn_layer_names since load_model is not invoked
    proposer.attn_layer_names = ["layer.0"]

    # Create input tensors
    cu_num_tokens = torch.tensor([0, seq_len_1, total_tokens],
                                 dtype=torch.int32,
                                 device=device)

    target_token_ids = torch.randint(0,
                                     vocab_size, (total_tokens, ),
                                     device=device)
    target_positions = torch.cat([
        torch.arange(seq_len_1, device=device),
        torch.arange(seq_len_2, device=device)
    ])
    target_hidden_states = torch.randn(total_tokens,
                                       hidden_size,
                                       device=device)
    target_slot_mapping = torch.randint(0,
                                        100, (total_tokens, ),
                                        device=device)
    next_token_ids = torch.randint(0,
                                   vocab_size, (batch_size, ),
                                   dtype=torch.int32,
                                   device=device)
    block_table = torch.randint(0, 10, (batch_size, 10), device=device)

    sampling_metadata = mock.MagicMock()

    # Call the method under test
    result = proposer.propose(target_token_ids=target_token_ids,
                              target_positions=target_positions,
                              target_hidden_states=target_hidden_states,
                              target_slot_mapping=target_slot_mapping,
                              next_token_ids=next_token_ids,
                              cu_num_tokens=cu_num_tokens,
                              block_table=block_table,
                              sampling_metadata=sampling_metadata)

    assert result.shape == (batch_size, num_speculative_tokens)

    # Create expected tokens based on our token pattern
    if num_speculative_tokens == 1:
        # Example for num_speculative_tokens=1:
        # [[42], [60]]
        expected_tokens = torch.tensor(
            [[base_token_ids[0]], [base_token_ids[1]]], device=device)
    else:
        # Example for num_speculative_tokens=3:
        # [[42, 43, 44], [60, 61, 62]]
        expected_tokens = torch.zeros((batch_size, num_speculative_tokens),
                                      dtype=torch.int64,
                                      device=device)
        for i in range(batch_size):
            for j in range(num_speculative_tokens):
                expected_tokens[i, j] = base_token_ids[i] + j

    # Verify all tokens match our expectations
    assert torch.equal(result, expected_tokens)
