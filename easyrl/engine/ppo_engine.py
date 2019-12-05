import time
from collections import deque
from itertools import chain
from itertools import count

import numpy as np
import torch
from torch.utils.data import DataLoader
from tqdm import tqdm

from easyrl.configs.ppo_config import ppo_cfg
from easyrl.engine.basic_engine import BasicEngine
from easyrl.utils.common import get_list_stats
from easyrl.utils.common import save_traj
from easyrl.utils.gae import cal_gae
from easyrl.utils.rl_logger import TensorboardLogger
from easyrl.utils.torch_util import EpisodeDataset
from easyrl.utils.torch_util import torch_to_np


class PPOEngine(BasicEngine):
    def __init__(self, agent, env, runner):
        super().__init__(agent=agent,
                         env=env,
                         runner=runner)
        self.cur_step = 0
        self._best_eval_ret = -np.inf
        self._eval_is_best = False
        if ppo_cfg.test or ppo_cfg.resume:
            self.cur_step = self.agent.load_model(step=ppo_cfg.resume_step)
        else:
            ppo_cfg.create_model_log_dir()
            self.train_ep_return = deque(maxlen=100)
        self.tf_logger = TensorboardLogger(log_dir=ppo_cfg.log_dir)

    def train(self):
        for iter_t in count():
            train_log_info = self.train_once()
            if iter_t % ppo_cfg.eval_interval == 0:
                eval_log_info, _ = self.eval()
                self.agent.save_model(is_best=self._eval_is_best,
                                      step=self.cur_step)
            else:
                eval_log_info = None
            if iter_t % ppo_cfg.log_interval == 0:
                if eval_log_info is not None:
                    train_log_info.update(eval_log_info)
                if ppo_cfg.linear_decay_lr:
                    train_log_info.update(self.agent.get_lr())
                if ppo_cfg.linear_decay_clip_range:
                    train_log_info.update(dict(clip_range=ppo_cfg.clip_range))
                scalar_log = {'scalar': train_log_info}
                self.tf_logger.save_dict(scalar_log, step=self.cur_step)
            if self.cur_step > ppo_cfg.max_steps:
                break
            if ppo_cfg.linear_decay_lr:
                self.agent.decay_lr()
            if ppo_cfg.linear_decay_clip_range:
                self.agent.decay_clip_range()

    @torch.no_grad()
    def eval(self, render=False, save_eval_traj=False, eval_num=1, sleep_time=0):
        time_steps = []
        rets = []
        ep_num = 0
        for idx in tqdm(range(eval_num), disable=not ppo_cfg.test):
            traj = self.runner(ppo_cfg.episode_steps,
                               return_on_done=True,
                               render=render,
                               sleep_time=sleep_time,
                               render_image=save_eval_traj)
            tsps = traj.steps_til_done.copy().tolist()
            rewards = traj.rewards
            for ej in range(traj.num_envs):
                ret = np.sum(rewards[:tsps[ej], ej])
                rets.append(ret)
            time_steps.extend(tsps)
            if save_eval_traj:
                ep_num = save_traj(traj, ppo_cfg.eval_dir, ep_num)

        raw_traj_info = {'return': rets,
                         'episode_length': time_steps}
        log_info = dict()
        for key, val in raw_traj_info.items():
            val_stats = get_list_stats(val)
            for sk, sv in val_stats.items():
                log_info['eval/' + key + '/' + sk] = sv
        if log_info['eval/return/mean'] > self._best_eval_ret:
            self._eval_is_best = True
            self._best_eval_ret = log_info['eval/return/mean']
        else:
            self._eval_is_best = False
        return log_info, raw_traj_info

    def train_once(self):
        t0 = time.perf_counter()
        self.agent.eval_mode()
        traj = self.runner(ppo_cfg.episode_steps)
        self.cur_step += traj.total_steps
        rewards = traj.rewards
        actions_info = traj.actions_info
        vals = np.array([ainfo['val'] for ainfo in actions_info])
        log_prob = np.array([ainfo['log_prob'] for ainfo in actions_info])
        with torch.no_grad():
            act_dist, last_val = self.agent.get_act_val(traj[-1].next_ob)
        adv = cal_gae(gamma=ppo_cfg.rew_discount,
                      lam=ppo_cfg.gae_lambda,
                      rewards=rewards,
                      value_estimates=vals,
                      last_value=torch_to_np(last_val),
                      dones=traj.dones)
        ret = adv + vals
        if ppo_cfg.normalize_adv:
            adv = adv.astype(np.float64)
            adv = (adv - np.mean(adv)) / (np.std(adv) + 1e-8)
        data = dict(
            ob=traj.obs,
            action=traj.actions,
            ret=ret,
            adv=adv,
            log_prob=log_prob,
            val=vals
        )
        rollout_dataset = EpisodeDataset(**data)
        rollout_dataloader = DataLoader(rollout_dataset,
                                        batch_size=ppo_cfg.batch_size,
                                        shuffle=True)
        optim_infos = []
        for oe in range(ppo_cfg.opt_epochs):
            for batch_ndx, batch_data in enumerate(rollout_dataloader):
                optim_info = self.agent.optimize(batch_data)
                optim_infos.append(optim_info)

        log_info = dict()
        for key in optim_infos[0].keys():
            log_info[key] = np.mean([inf[key] for inf in optim_infos])
        t1 = time.perf_counter()
        actions_stats = get_list_stats(traj.actions)
        for sk, sv in actions_stats.items():
            log_info['rollout_action/' + sk] = sv
        log_info['time_per_iter'] = t1 - t0
        log_info['rollout_steps_per_iter'] = traj.total_steps
        ep_returns = list(chain(*traj.episode_returns))
        for epr in ep_returns:
            self.train_ep_return.append(epr)
        ep_returns_stats = get_list_stats(self.train_ep_return)
        for sk, sv in ep_returns_stats.items():
            log_info['episode_return/' + sk] = sv
        train_log_info = dict()
        for key, val in log_info.items():
            train_log_info['train/' + key] = val
        # histogram_log = {'histogram': {'rollout_action': traj.actions}}
        # self.tf_logger.save_dict(histogram_log, step=self.cur_step)
        return train_log_info
