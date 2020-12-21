from dataclasses import dataclass

import numpy as np
import torch

from easyrl.agents.ppo_agent import PPOAgent
from easyrl.configs import cfg
from easyrl.utils.torch_util import action_entropy
from easyrl.utils.torch_util import action_from_dist
from easyrl.utils.torch_util import action_log_prob
from easyrl.utils.torch_util import torch_float
from easyrl.utils.torch_util import torch_to_np


@dataclass
class PPORNNAgent(PPOAgent):

    @torch.no_grad()
    def get_action(self, ob, sample=True, hidden_state=None, prev_done=False, *args, **kwargs):
        self.eval_mode()
        hidden_state = self.check_hidden_state(hidden_state, prev_done)

        t_ob = torch.from_numpy(ob).float().to(cfg.alg.device).unsqueeze(dim=1)
        act_dist, val, out_hidden_state = self.get_act_val(t_ob,
                                                           hidden_state=hidden_state)
        action = action_from_dist(act_dist,
                                  sample=sample)
        log_prob = action_log_prob(action, act_dist)
        entropy = action_entropy(act_dist, log_prob)
        action_info = dict(
            log_prob=torch_to_np(log_prob.squeeze(1)),
            entropy=torch_to_np(entropy.squeeze(1)),
            val=torch_to_np(val.squeeze(1)),
        )
        return torch_to_np(action.squeeze(1)), action_info, out_hidden_state

    def get_act_val(self, ob, hidden_state=None, done=None, *args, **kwargs):
        ob = torch_float(ob, device=cfg.alg.device)
        act_dist, body_out, out_hidden_state = self.actor(ob,
                                                          hidden_state=hidden_state,
                                                          done=done)
        if self.same_body:
            val, body_out, out_hidden_state = self.critic(body_x=body_out,
                                                          hidden_state=hidden_state,
                                                          done=done)
        else:
            val, body_out, out_hidden_state = self.critic(x=ob,
                                                          hidden_state=hidden_state,
                                                          done=done)
        val = val.squeeze(-1)
        return act_dist, val, out_hidden_state

    @torch.no_grad()
    def get_val(self, ob, hidden_state=None, prev_done=False, *args, **kwargs):
        self.eval_mode()
        hidden_state = self.check_hidden_state(hidden_state, prev_done)

        ob = torch_float(ob, device=cfg.alg.device).unsqueeze(dim=1)
        val, body_out, out_hidden_state = self.critic(x=ob,
                                                      hidden_state=hidden_state)
        val = val.squeeze(-1)
        return val, out_hidden_state

    def optim_preprocess(self, data):
        self.train_mode()
        for key, val in data.items():
            data[key] = torch_float(val, device=cfg.alg.device)
        ob = data['ob']
        action = data['action']
        ret = data['ret']
        adv = data['adv']
        old_log_prob = data['log_prob']
        old_val = data['val']
        done = data['done']

        act_dist, val, out_hidden_state = self.get_act_val(ob, done=done)
        log_prob = action_log_prob(action, act_dist)
        entropy = action_entropy(act_dist, log_prob)
        processed_data = dict(
            val=val,
            old_val=old_val,
            ret=ret,
            log_prob=log_prob,
            old_log_prob=old_log_prob,
            adv=adv,
            entropy=entropy
        )
        return processed_data

    def check_hidden_state(self, hidden_state, prev_done):
        if prev_done is not None:
            # if the last step is the end of an episode,
            # then reset hidden state
            done_idx = np.argwhere(prev_done).flatten()
            if done_idx.size > 0:
                ld, b, hz = hidden_state.shape
                hidden_state[:, done_idx] = torch.zeros(ld, done_idx.size, hz,
                                                        device=hidden_state.device)
        return hidden_state
