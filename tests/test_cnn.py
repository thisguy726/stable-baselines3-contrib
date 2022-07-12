import os
from copy import deepcopy

import numpy as np
import pytest
import torch as th
from stable_baselines3.common.envs import FakeImageEnv
from stable_baselines3.common.utils import zip_strict
from stable_baselines3.common.vec_env import VecTransposeImage, is_vecenv_wrapped

from sb3_contrib import QRDQN, TQC, TRPO


@pytest.mark.parametrize("model_class", [TQC, QRDQN, TRPO])
def test_cnn(tmp_path, model_class):
    SAVE_NAME = "cnn_model.zip"
    # Fake grayscale with frameskip
    # Atari after preprocessing: 84x84x1, here we are using lower resolution
    # to check that the network handle it automatically
    env = FakeImageEnv(
        screen_height=40,
        screen_width=40,
        n_channels=1,
        discrete=model_class not in {TQC},
    )
    kwargs = {}
    if model_class in {TQC, QRDQN}:
        # Avoid memory error when using replay buffer
        # Reduce the size of the features and the number of quantiles
        kwargs = dict(
            buffer_size=250,
            policy_kwargs=dict(
                n_quantiles=25,
                features_extractor_kwargs=dict(features_dim=32),
            ),
        )
    model = model_class("CnnPolicy", env, **kwargs).learn(250)

    obs = env.reset()

    # FakeImageEnv is channel last by default and should be wrapped
    assert is_vecenv_wrapped(model.get_env(), VecTransposeImage)

    # Test stochastic predict with channel last input
    if model_class == QRDQN:
        model.exploration_rate = 0.9

    for _ in range(10):
        model.predict(obs, deterministic=False)

    action, _ = model.predict(obs, deterministic=True)

    model.save(tmp_path / SAVE_NAME)
    del model

    model = model_class.load(tmp_path / SAVE_NAME)

    # Check that the prediction is the same
    assert np.allclose(action, model.predict(obs, deterministic=True)[0])

    os.remove(str(tmp_path / SAVE_NAME))


def patch_qrdqn_names_(model):
    # Small hack to make the test work with QRDQN
    if isinstance(model, QRDQN):
        model.critic = model.quantile_net
        model.critic_target = model.quantile_net_target


def params_should_match(params, other_params):
    for param, other_param in zip_strict(params, other_params):
        assert th.allclose(param, other_param)


def params_should_differ(params, other_params):
    for param, other_param in zip_strict(params, other_params):
        assert not th.allclose(param, other_param)


@pytest.mark.parametrize("model_class", [TQC, QRDQN])
@pytest.mark.parametrize("share_features_extractor", [True, False])
def test_feature_extractor_target_net(model_class, share_features_extractor):
    if model_class == QRDQN and share_features_extractor:
        pytest.skip()

    env = FakeImageEnv(screen_height=40, screen_width=40, n_channels=1, discrete=model_class not in {TQC})

    if model_class in {TQC, QRDQN}:
        # Avoid memory error when using replay buffer
        # Reduce the size of the features and the number of quantiles
        kwargs = dict(
            buffer_size=250,
            learning_starts=100,
            policy_kwargs=dict(n_quantiles=25, features_extractor_kwargs=dict(features_dim=32)),
        )
    if model_class != QRDQN:
        kwargs["policy_kwargs"]["share_features_extractor"] = share_features_extractor

    model = model_class("CnnPolicy", env, seed=0, **kwargs)

    patch_qrdqn_names_(model)

    if share_features_extractor:
        # Check that the objects are the same and not just copied
        assert id(model.policy.actor.features_extractor) == id(model.policy.critic.features_extractor)
    elif model_class != QRDQN:
        assert id(model.policy.actor.features_extractor) != id(model.policy.critic.features_extractor)

    # Critic and target should be equal at the begginning of training
    params_should_match(model.critic.parameters(), model.critic_target.parameters())

    model.learn(200)

    # Critic and target should differ
    params_should_differ(model.critic.parameters(), model.critic_target.parameters())

    # Re-initialize and collect some random data (without doing gradient steps)
    model = model_class("CnnPolicy", env, seed=0, **kwargs).learn(10)

    patch_qrdqn_names_(model)

    original_param = deepcopy(list(model.critic.parameters()))
    original_target_param = deepcopy(list(model.critic_target.parameters()))

    # Deactivate copy to target
    model.tau = 0.0
    model.train(gradient_steps=1)

    # Target should be the same
    params_should_match(original_target_param, model.critic_target.parameters())

    # not the same for critic net (updated by gradient descent)
    params_should_differ(original_param, model.critic.parameters())

    # Update the reference as it should not change in the next step
    original_param = deepcopy(list(model.critic.parameters()))

    # Deactivate learning rate
    model.lr_schedule = lambda _: 0.0
    # Re-activate polyak update
    model.tau = 0.01
    # Special case for QRDQN: target net is updated in the `collect_rollouts()`
    # not the `train()` method
    if model_class == QRDQN:
        model.target_update_interval = 1
        model._on_step()

    model.train(gradient_steps=1)

    # Target should have changed now (due to polyak update)
    params_should_differ(original_target_param, model.critic_target.parameters())

    # Critic should be the same
    params_should_match(original_param, model.critic.parameters())
