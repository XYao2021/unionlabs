#%% Import packages -----------------------------------------------------------
import os
import numpy as np
import torch
import torch.nn as nn
from torch.distributions.categorical import Categorical

#%% defines -------------------------------------------------------------------
def _layer_init(layer, std=np.sqrt(2), bias_const=0.0):
    """Orthogonal initialization for better learning."""
    nn.init.orthogonal_(layer.weight, std)
    nn.init.constant_(layer.bias, bias_const)
    return layer

def actor_MLP(input_dim: int, hidden_width: int, hidden_depth: int, output_dim: int, ac_type):
    layers = []
    layers.append(_layer_init(nn.Linear(input_dim, hidden_width)))
    layers.append(nn.LayerNorm(hidden_width))
    layers.append(nn.ReLU())
    for _ in range(hidden_depth):
        layers.append(_layer_init(nn.Linear(hidden_width, hidden_width)))
        layers.append(nn.LayerNorm(hidden_width))
        layers.append(nn.ReLU())
    layers.append(_layer_init(nn.Linear(hidden_width, output_dim), std=0.01))
    if ac_type == 0: # A2C
        layers.append(nn.Softmax(dim=-1))

    return nn.Sequential(*layers)

def critic_MLP(input_dim: int, hidden_width: int, hidden_depth: int):
    layers = []
    layers.append(_layer_init(nn.Linear(input_dim, hidden_width)))
    layers.append(nn.LayerNorm(hidden_width))
    layers.append(nn.ReLU())
    for _ in range(hidden_depth):
        layers.append(_layer_init(nn.Linear(hidden_width, hidden_width)))
        layers.append(nn.LayerNorm(hidden_width))
        layers.append(nn.ReLU())
    layers.append(_layer_init(nn.Linear(hidden_width, 1), std=1.0))

    return nn.Sequential(*layers)

class ResidualBlock(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.linear1 = nn.Linear(dim, dim)
        self.relu1 = nn.ReLU()
        self.linear2 = nn.Linear(dim, dim)
        self.norm = nn.LayerNorm(dim)
        self.relu2 = nn.ReLU()

    def forward(self, x):
        out = self.linear1(x)
        out = self.relu1(out)
        out = self.linear2(out)
        out += x
        out = self.norm(out)
        out = self.relu2(out)
        return out

def actor_ResNet(input_dim: int, hidden_width: int, hidden_depth: int, output_dim: int, ac_type):
    layers = []
    layers.append(nn.Linear(input_dim, hidden_width))
    for _ in range(hidden_depth):
        layers.append(ResidualBlock(hidden_width))
    layers.append(nn.Linear(hidden_width, output_dim))
    if ac_type == 0: # A2C
        layers.append(nn.Softmax(dim=-1))

    return nn.Sequential(*layers)

def critic_ResNet(input_dim: int, hidden_width: int, hidden_depth: int):
    layers = []
    layers.append(nn.Linear(input_dim, hidden_width))
    for _ in range(hidden_depth):
        layers.append(ResidualBlock(hidden_width))
    layers.append(nn.Linear(hidden_width, 1))

    return nn.Sequential(*layers)

class Actor_A2C(nn.Module):
    def __init__(self, input_dim, hidden_width, hidden_depth, output_dim, net_type, ac_type, cf):
        super().__init__()
        if net_type == 0:
            self.actor_net = actor_MLP(input_dim, hidden_width, hidden_depth, output_dim, ac_type)
        elif net_type == 1:
            self.actor_net = actor_ResNet(input_dim, hidden_width, hidden_depth, output_dim, ac_type)

    def forward(self, tensor_in):
        pi = self.actor_net(tensor_in)
        return pi

class Actor_PPO(nn.Module):
    def __init__(self, input_dim, hidden_width, hidden_depth, output_dim, net_type, ac_type, cf):
        super().__init__()
        if net_type == 0:
            self.actor_net = actor_MLP(input_dim, hidden_width, hidden_depth, output_dim, ac_type)
        elif net_type == 1:
            self.actor_net = actor_ResNet(input_dim, hidden_width, hidden_depth, output_dim, ac_type)

    def forward(self, tensor_in, action=None):
        logits = self.actor_net(tensor_in)
        probs = Categorical(logits=logits)

        if action is None:
            action = probs.sample()

        return action, probs.log_prob(action), probs.entropy(), probs.probs.detach()

class Actor_SAC(nn.Module):
    def __init__(self, input_dim, hidden_width, hidden_depth, output_dim, net_type, ac_type, cf):
        super().__init__()
        self.log_std_min = cf.log_std_min
        self.log_std_max = cf.log_std_max

        if net_type == 0:
            self.actor_net = actor_MLP(input_dim, hidden_width, hidden_depth, output_dim * 2, ac_type)
        elif net_type == 1:
            self.actor_net = actor_ResNet(input_dim, hidden_width, hidden_depth, output_dim * 2, ac_type)

    def forward(self, tensor_in):
        mean, log_std = self.actor_net(tensor_in).chunk(2, dim=-1)
        # log_std = torch.clamp(log_std, -20, 2)
        log_std = torch.tanh(log_std)
        log_std = self.log_std_min + 0.5 * (
            self.log_std_max - self.log_std_min
        ) * (log_std + 1)
        std = log_std.exp()
        normal = torch.distributions.Normal(mean, std)
        x_t = normal.rsample()  # reparameterization trick

        y_t = torch.tanh(x_t)
        log_prob = normal.log_prob(x_t) - torch.log(1 - y_t.pow(2) + 1e-6)
        log_prob = log_prob.sum(dim=-1, keepdim=True)

        return y_t, log_prob, mean, std

class Critic(nn.Module):
    def __init__(self, input_dim, hidden_width, hidden_depth, net_type, cf):
        super().__init__()
        if net_type == 0:
            self.critic_net = critic_MLP(input_dim, hidden_width, hidden_depth)
        elif net_type == 1:
            self.critic_net = critic_ResNet(input_dim, hidden_width, hidden_depth)

    def forward(self, tensor_in):
        val = self.critic_net(tensor_in)
        return val

class ReplayBuffer(object):
    def __init__(self, obs_shape, output_shape, action_shape, state_shape, capacity, batch_size, dim_O, dim_A, dim_H, device):
        self.capacity = capacity
        self.batch_size = batch_size
        self.device = device

        self.obses = np.empty((capacity, *obs_shape), dtype=np.float32)
        self.outputs = np.empty((capacity, *output_shape), dtype=np.float32)
        self.actions = np.empty((capacity, *action_shape), dtype=np.float32)
        self.rewards = np.empty((capacity, 1), dtype=np.float32)
        self.next_obses = np.empty((capacity, *obs_shape), dtype=np.float32)
        self.next_actions = np.empty((capacity, *action_shape), dtype=np.float32)
        self.timesteps = np.empty((capacity, 1), dtype=np.int64)
        self.dones = np.empty((capacity, 1), dtype=np.int64)

        self.states = np.empty((capacity, *state_shape), dtype=np.float32)
        self.next_states = np.empty((capacity, *state_shape), dtype=np.float32)

        self.idx = 0
        self.last_save = 0
        self.full = False

        self.dim_O = dim_O
        self.dim_A = dim_A
        self.dim_H = dim_H

    def add(self, obs, output, action, reward, next_obs, next_action, timestep, state, next_state, done):
        np.copyto(self.obses[self.idx], obs)
        np.copyto(self.outputs[self.idx], output)
        np.copyto(self.actions[self.idx], action)
        np.copyto(self.rewards[self.idx], reward)
        np.copyto(self.next_obses[self.idx], next_obs)
        np.copyto(self.next_actions[self.idx], next_action)
        np.copyto(self.timesteps[self.idx], timestep)
        np.copyto(self.dones[self.idx], done)

        np.copyto(self.states[self.idx], state)
        np.copyto(self.next_states[self.idx], next_state)

        self.idx = (self.idx + 1) % self.capacity
        self.full = self.full or self.idx == 0

    def sample(self):
        idxs = np.random.randint(0, self.capacity if self.full else self.idx, size=self.batch_size)

        obses = torch.as_tensor(self.obses[idxs], device=self.device).float()
        outputs = torch.as_tensor(self.outputs[idxs], device=self.device).float()
        actions = torch.as_tensor(self.actions[idxs], device=self.device)
        rewards = torch.as_tensor(self.rewards[idxs], device=self.device)
        next_obses = torch.as_tensor(self.next_obses[idxs], device=self.device).float()
        next_actions = torch.as_tensor(self.next_actions[idxs], device=self.device)
        timesteps = torch.as_tensor(self.timesteps[idxs], device=self.device)

        return obses, outputs, actions, rewards, next_obses, next_actions, timesteps

    def history_sample(self, M):
        idxs = np.random.randint(2*M, self.capacity if self.full else self.idx, size=self.batch_size)

        hists, next_hists = [], []
        for idx in idxs:
            obses = self.obses[idx-M:idx+1]
            actions = self.actions[idx-M:idx+1]
            hist = np.concatenate((obses, actions), axis=1).reshape([-1])[:-self.dim_A]
            hists.append(np.concatenate((np.zeros([self.dim_H+self.dim_O-len(hist),]), hist), axis=0))

            next_obses = self.next_obses[idx-M:idx+1]
            next_actions = self.next_actions[idx-M:idx+1]
            next_hist = np.concatenate((next_obses, next_actions), axis=1).reshape([-1])[:-self.dim_A]
            next_hists.append(np.concatenate((np.zeros([self.dim_H+self.dim_O-len(next_hist),]), next_hist), axis=0))

        hists = torch.as_tensor(np.array(hists), device=self.device).float()
        outputs = torch.as_tensor(self.outputs[idxs], device=self.device).float()
        rewards = torch.as_tensor(self.rewards[idxs], device=self.device)
        next_hists = torch.as_tensor(np.array(next_hists), device=self.device).float()
        dones = torch.as_tensor(self.dones[idxs], device=self.device)

        return hists, outputs, rewards, next_hists, dones

    def save(self, save_dir, idx, cf, cf_L, run):
        path = os.path.join(save_dir, f'D{cf.num_D}_O{cf.objective}/A{idx}_D{cf.num_D}_M{cf_L.M}_O{cf.objective}_R{run}-{self.last_save}_{self.idx}.pt')
        payload = [
            self.obses[self.last_save:self.idx],
            self.outputs[self.last_save:self.idx],
            self.actions[self.last_save:self.idx],
            self.rewards[self.last_save:self.idx],
            self.next_obses[self.last_save:self.idx],
            self.next_actions[self.last_save:self.idx],
            self.timesteps[self.last_save:self.idx],
            self.dones[self.last_save:self.idx],
            self.states[self.last_save:self.idx],
            self.next_states[self.last_save:self.idx],
        ]
        torch.save(payload, path)

    def load(self, load_dir, idx, cf, cf_F):
        print(f"Loading for agent {idx}")
        load_dir = os.path.join(load_dir, f'D{cf.num_D}_O{cf.objective}')
        chunks = os.listdir(load_dir)
        filtered_chunks = [x for x in chunks if f"A{idx}" in x]
        filtered_chunks = sorted(filtered_chunks, key=lambda x: int(x.split('-')[1].split('_')[0]))
        for a, chunk in enumerate(filtered_chunks):
            if (a+1) <= cf_F.fm_run:
                print(f'File {chunk}')
                start, end = [int(x) for x in chunk.split('-')[1].split('.')[0].split('_')]
                start = start + self.idx
                end = end + self.idx
                path = os.path.join(load_dir, chunk)
                payload = torch.load(path)
                assert self.idx == start
                self.obses[start:end] = payload[0]
                self.outputs[start:end] = payload[1]
                self.actions[start:end] = payload[2]
                self.rewards[start:end] = payload[3]
                self.next_obses[start:end] = payload[4]
                self.next_actions[start:end] = payload[5]
                self.timesteps[start:end] = payload[6]
                self.dones[start:end] = payload[7]
                self.states[start:end] = payload[8]
                self.next_states[start:end] = payload[9]
                self.idx = end

def assign_params_from_vector(model, vector):
    idx = 0
    for param in model.parameters():
        numel = param.numel()
        param.data = vector[idx:idx + numel].view_as(param).data
        idx += numel

def soft_update_params(net, target_net, tau):
    for param, target_param in zip(net.parameters(), target_net.parameters()):
        target_param.data.copy_(tau * param.data + (1 - tau) * target_param.data)

def agent_update(agent, idx, obs_curr_collect, obs_next_collect, action_curr_collect, reward, done, cf_L):

    V_curr = agent.critic(obs_curr_collect)
    with torch.no_grad():
        V_next = agent.critic(obs_next_collect)
    TD_err = reward + cf_L.GAMMA * (1-done) * V_next - V_curr

    out_Critic = np.array(V_curr.tolist()) # output of the critic
    advantage = TD_err.detach()
    out_TD = np.array(advantage.tolist())

    critic_loss = TD_err.pow(2).mean()
    agent.critic_optimizer.zero_grad()
    critic_loss.backward()
    critic_grad_norm = torch.nn.utils.clip_grad_norm_(agent.critic.parameters(), cf_L.critic_clip).item()
    agent.critic_optimizer.step()

    output_curr = agent.actor(obs_curr_collect)
    out_Actor = np.array(output_curr.tolist()) # output of the actor

    if cf_L.action_type == 0:
        for a in range(cf_L.num_B):
            if output_curr[a][action_curr_collect[a]] < 0.0001:
                print(f"History {idx}, {action_curr_collect[a]}, {output_curr[a][action_curr_collect[a]].item()}")
        actor_loss = -torch.log(torch.gather(output_curr, 1, action_curr_collect.to(torch.int64)).clamp_min(0.0001)) * advantage # discrete action
    elif cf_L.action_type == 1:
        actor_loss = -torch.log(output_curr) * advantage # continuous action
    agent.actor_optimizer.zero_grad()
    actor_loss.mean().backward()
    # d_loss = [actor_loss.item(), output_curr[A_curr].item(), TD_err]
    # d_norm = [param.grad.norm().item() for param in actor.parameters() if param.grad is not None]
    actor_grad_norm = torch.nn.utils.clip_grad_norm_(agent.actor.parameters(), cf_L.actor_clip).item()
    agent.actor_optimizer.step()

    # return out_Critic, out_TD, out_Actor, critic_grad_norm, actor_grad_norm, d_loss, d_norm
    return out_Critic, out_TD, out_Actor, critic_grad_norm, actor_grad_norm

def compute_gae(rewards, values_curr, values_next, dones, gamma, gae_lambda, cf_L):
    """Compute Generalized Advantage Estimation."""
    advantages = torch.zeros_like(rewards)
    delta = torch.zeros_like(rewards)
    lastgaelam = 0

    for a in range(rewards.size()[0]):
        for t in reversed(range(rewards.size()[1])):
            if (cf_L.MARL_type == 0):
                delta[a, t] = rewards[a, t] + gamma * values_next[a, t] * (1.0 - dones[a, t]) - values_curr[a, t]
            elif (cf_L.MARL_type == 1):
                delta[a, t] = rewards[a, t] + gamma * values_next[t] * (1.0 - dones[a, t]) - values_curr[t]
            advantages[a, t] = lastgaelam = delta[a, t] + gamma * gae_lambda * (1.0 - dones[a, t]) * lastgaelam

    returns = advantages + values_curr
    return advantages, returns, delta
