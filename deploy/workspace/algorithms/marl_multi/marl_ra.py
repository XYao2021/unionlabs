#%% Import packages -----------------------------------------------------------
import os
import time
import pickle
import argparse
import numpy as np
import torch
import marl_setting as SF
import marl_base as BF
import marl_network as NF
import marl_learning as LF

#%% Setting -------------------------------------------------------------------
save_file = 1 # whether to save the result
save_buffer = 0 # whether to save the buffer for each agent

num_Ds = [2, 4, 8] # num. of devices
num_Es = [400, 400, 5000] # num. of episodes for learning
actor_rates = [4e-4, 4e-4, 4e-4] # actor update rate
critic_rates = [4e-4, 4e-4, 4e-4] # critic update rate

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--scn_idx', type=int, default=0, help='Scenario Index')
    parser.add_argument('--dev_type', type=int, default=0, help='Device type')
    parser.add_argument('--objective', type=int, default=0, help='Optimization Objective')
    parser.add_argument('--marl', type=int, default=0, help='MARL type')
    parser.add_argument('--ac', type=int, default=0, help='Actor-critic type')
    parser.add_argument('--debug', type=int, default=0, help='Whether to run the code in a debug mode')
    parser.add_argument('--offset', type=int, default=0, help='Offset to distinguish saved files')
    parser.add_argument('--num_run', type=int, default=5, help='Number of simulation runs')
    args = parser.parse_args()
    return args
args = parse_args()

cf = SF.SimSettings() # SimSettings
cf.device_type = args.dev_type
cf.objective = args.objective
cf.num_D = num_Ds[args.scn_idx]
cf.num_R = args.num_run
cf.num_E = num_Es[args.scn_idx]
cf.num_N = int(cf.num_D/2)
cf.update_int = int(cf.num_E*cf.update_ratio) # no update if greater than cf.num_E

if cf.fixed_seed:
    BF.set_seed_everywhere(cf.seed_idx)

cf_N = SF.NetworkSettings() # NetworkSettings
cf_N.transmit_prob = 1/cf.num_D # transmit prob for probability-based transmission

cf_L = SF.LearningSettings() # LearningSettings
cf_L.MARL_type = args.marl # 0 for DTDE, 1 for CTDE
cf_L.AC_type = args.ac # 0 for A2C, 1 for PPO
cf_L.actor_rate = actor_rates[args.scn_idx] # actor update rate
cf_L.critic_rate = critic_rates[args.scn_idx] # critic update rate
cf_L.dim_S = 2*cf.num_D + 1 # state space dimension [E_time (num_D), queue (num_D), ch_usage (1)]
cf_L.dim_O = cf.num_D + 2 # observation space dimension [E_time (num_D), queue (1), ch_usage (1)]
cf_L.dim_H = cf_L.M*(cf_L.dim_O + 1) # num. of history * (observation + action)
cf_L.buffer_capacity = int((cf.num_S * cf.num_E) / 20) * 10 # about 10 scenario runs

args_dppo = SF.DPPO_Args() # DPPO
if cf_L.MARL_type == 1:
    args_dppo.agent_com = False

device_names = ['Agent', 'q-ALOHA', 'CSMA/CA', 'Q-CSMA']
initial_dist = np.ones([cf.num_D,])
# initial_dist = np.linspace(2, 14, cf.num_D)
# print(f"Device distance = {initial_dist}")

device_types = [cf.device_type for _ in range(cf.num_D)]
num_A = device_types.count(0) # num. of agents
num_L = device_types.count(1) + device_types.count(2) + device_types.count(3) # num. of legacy devices
ids_agent = [i for i, x in enumerate(device_types) if x == 0]
ids_legacy = [i for i, x in enumerate(device_types) if x != 0]

torch_device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

if num_A != 0:
    A_consensus = torch.as_tensor(NF.consensus(num_A, cf.num_N, cf.num_G), dtype=torch.float32).to(torch_device)

# Print Information -----------------------------------------------------------
print(cf)
print(cf_N)
print(cf_L)
print(args)
print("---------------------------------")
print(f"Device = {torch_device}")
work_dir = 'data/'
file_name = "RA_ND%i_DT%i_MT%i_AT%i_NT%i_W%i_D%i_E%i_S%i_r%i_%i_%i_B%i_R%i_%i" %(cf.num_D, cf.device_type,
           cf_L.MARL_type, cf_L.AC_type, cf_L.net_type, cf_L.dim_W, cf_L.dim_D, cf.num_E, cf.num_S, cf_L.actor_rate*10000000, cf_L.critic_rate*10000000, cf_L.GAMMA*100,
           cf_L.num_B, cf.num_R, args.offset)
print(file_name)
print(f"dim_S = {cf_L.dim_S}, dim_O = {cf_L.dim_O}, dim_H = {cf_L.dim_H}")
print(f"Actor in = {cf_L.dim_H + cf_L.dim_O}, Dist. Critic in = {cf_L.dim_H + cf_L.dim_O}, Cent. Critic in = {cf_L.dim_H*num_A + cf_L.dim_O*num_A}")
print(f"Num. of CPU cores available: {os.cpu_count()}")
print(f"Objective = {cf.objective}")

# Assert ----------------------------------------------------------------------
assert len(device_types) == cf.num_D

#%% Initializations -----------------------------------------------------------

# BF.make_dir(work_dir)
# BF.make_dir(os.path.join(work_dir, f'D{cf.num_D}_O{cf.objective}'))

# value appended at the end of each episode (R x D x E)
Pkt_tot = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_R)]
Pkt_coll = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_R)]
Pkt_suc = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_R)]
Pkt_del = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_R)]
R_avgs = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_R)]
Tputs = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_R)]
Update_Ts = [[] for _ in range(cf.num_R)] # (R x E)
Total_Ts = [[] for _ in range(cf.num_R)] # (R x E)

#%% Main ----------------------------------------------------------------------

for rnd in range(cf.num_R):
    devices = []
    idxes = np.zeros([len(device_names),], dtype=int)
    for a in range(cf.num_D):
        devices.append(NF.RA_device(a, device_types[a], idxes[device_types[a]], cf, cf_N, cf_L, torch_device, initial_dist=initial_dist[a]))
        idxes[device_types[a]] += 1

    if (cf_L.MARL_type == 1) & (num_A != 0):
        cent_critic = LF.Critic(cf_L.dim_H*num_A + cf_L.dim_O*num_A, cf_L.dim_W*num_A, cf_L.dim_D, cf_L.net_type, cf_L).to(torch_device)
        cent_critic_optimizer = torch.optim.Adam(cent_critic.parameters(), lr=cf_L.critic_rate, eps=1e-5)

    if rnd == 0:
        for a in range(cf.num_D):
            print(f"Device {devices[a].device_id+1} - {device_names[devices[a].device_type]} {devices[a].type_id+1}")

        if device_types[0] == 0:
            # print(devices[0].actor)
            print(f"Num. of Actor Parameters = {sum(p.numel() for p in devices[0].actor.parameters() if p.requires_grad)}")
            if (cf_L.MARL_type == 0):
                # print(devices[0].critic)
                print(f"Num. of Critic Parameters = {sum(p.numel() for p in devices[0].critic.parameters() if p.requires_grad)}")
            elif (cf_L.MARL_type == 1):
                # print(cent_critic)
                print(f"Num. of Central Critic Parameters = {sum(p.numel() for p in cent_critic.parameters() if p.requires_grad)}")

    obs_curr_collect = torch.zeros([num_A, cf_L.num_B, cf_L.dim_H + cf_L.dim_O]).to(torch_device)
    obs_next_collect = torch.zeros([num_A, cf_L.num_B, cf_L.dim_H + cf_L.dim_O]).to(torch_device)
    action_curr_collect = torch.zeros([num_A, cf_L.num_B, 1], dtype=torch.int).to(torch_device)
    reward_collect = torch.zeros([num_A, cf_L.num_B, 1]).to(torch_device)
    done_collect = torch.zeros([num_A, cf_L.num_B, 1]).to(torch_device)
    if cf_L.AC_type == 1:
        logprob_collect = torch.zeros([num_A, cf_L.num_B, 1]).to(torch_device)
        if (cf_L.MARL_type == 0):
            value_curr_collect = torch.zeros([num_A, cf_L.num_B, 1]).to(torch_device)
            value_next_collect = torch.zeros([num_A, cf_L.num_B, 1]).to(torch_device)
        elif (cf_L.MARL_type == 1):
            value_curr_collect = torch.zeros([cf_L.num_B, 1]).to(torch_device)
            value_next_collect = torch.zeros([cf_L.num_B, 1]).to(torch_device)

    # value appended at each time step
    States = [[] for _ in range(cf.num_E)] # state (E x S x dim_S)
    if args.debug == 1:
        Ch_uses = [[] for _ in range(cf.num_E)] # channel use status (E x S)
        C_flags = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_E)] # CCA flag (E x D x S)
        C_cnts = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_E)] # CCA count (E x D x S)
        B_flags = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_E)] # backoff flag (E x D x S)
        B_times = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_E)] # backoff time (E x D x S)
        B_cnts = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_E)] # backoff count (E x D x S)
        T_flags = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_E)] # transmission flag (E x D x S)
        T_cnts = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_E)] # transmission count (E x D x S)
        A_flags = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_E)] # ACK flag (E x D x S)
        A_cnts = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_E)] # ACK count (E x D x S)
        Ques = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_E)] # queue (E x D x S)
        E_times = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_E)] # elapsed time (E x D x S)

    # stored only for each action taken
    Update_times = [[] for _ in range(cf.num_E)] # Update runtimes (E x ??)
    Actions = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_E)] # action (E x D x ??)
    Rewards = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_E)] # reward (E x D x ??)
    R_sums = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_E)] # cumulative reward (E x D x ??)
    if (cf_L.MARL_type == 0):
        Out_Critic = [[[] for _ in range(num_A)] for _ in range(cf.num_E)] # Critic output (E x A x ??)
    elif (cf_L.MARL_type == 1):
        Out_Critic = [[] for _ in range(cf.num_E)] # Critic output (E x A x ??)
    Out_Actor = [[[] for _ in range(num_A)] for _ in range(cf.num_E)] # Actor output (E x A x ?? x dim_A)
    Out_TD = [[[] for _ in range(num_A)] for _ in range(cf.num_E)] # Actor output (E x A x ?? x dim_A)

    # stored only for each successful transmission
    dels = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_E)] # packet delay (E x D x ??)
    datas = [[[] for _ in range(cf.num_D)] for _ in range(cf.num_E)] # packet delay (E x D x ??)

    if (cf_L.MARL_type == 0):
        critic_grad_norms = [[] for _ in range(num_A)]
    elif (cf_L.MARL_type == 1):
        critic_grad_norms = []
    actor_grad_norms = [[] for _ in range(num_A)]

    print(f"Run ({rnd+1}/{cf.num_R})")

    for epi in range(cf.num_E):
        epi_start_time = time.time()

        if ((epi+1) % 50 == 0) & (len(ids_agent) > 0):
            pkt_sum = np.sum([Pkt_suc[rnd][a][-1] for a in range(cf.num_D)])
#             print(f'Episode {epi+1} - {pkt_sum:.3f} / {np.mean([Tputs[rnd][a][-1] for a in range(cf.num_D)])*cf.num_D/1e6:.3f} \
# / {np.mean([Pkt_coll[rnd][a][-1] for a in range(cf.num_D)]):.3f}, {devices[0].replay_buffer.idx}')
            print(f'Episode {epi+1} - {pkt_sum:.3f} / {np.sum([Pkt_suc[rnd][a][-1] for a in range(cf.num_D)])*cf_N.pkt_len*8/(cf.num_S*cf_N.Ts)/1e6:.3f} \
/ {np.mean([Pkt_coll[rnd][a][-1] for a in range(cf.num_D)]):.3f}, {devices[0].replay_buffer.idx}')

        if (epi+1) % cf.update_int == 0:
            print(f"Consensus update at {epi+1}")
            A_consensus = torch.as_tensor(BF.consensus(num_A, cf.num_N, cf.num_G, shuffle=True), dtype=torch.float32).to(torch_device)

        for a in range(cf.num_D):
            devices[a].reset(cf_N, initial_dist[a])

        debug_norm = [[] for _ in range(num_A)]
        debug_loss = [[] for _ in range(num_A)]
        debug_hist = [[] for _ in range(num_A)]

        ch_usage = 0
        done = []
        first_action = np.ones([cf.num_D, ])
        state_curr = np.zeros([cf.num_D, cf_L.dim_S])
        state_next = np.zeros([cf.num_D, cf_L.dim_S])
        obs_curr = np.zeros([cf.num_D, cf_L.dim_O])
        obs_next = np.zeros([cf.num_D, cf_L.dim_O])
        out_curr = np.zeros([cf.num_D, cf_L.dim_A])
        out_next = np.zeros([cf.num_D, cf_L.dim_A])
        action_curr = np.zeros([cf.num_D, ], dtype=int) - 1
        action_next = np.zeros([cf.num_D, ], dtype=int) - 1
        logprob_curr = np.zeros([cf.num_D, ])
        logprob_next = np.zeros([cf.num_D, ])
        entropy_curr = np.zeros([cf.num_D, ])
        entropy_next = np.zeros([cf.num_D, ])
        if (cf_L.MARL_type == 0):
            value_curr = np.zeros([cf.num_D, ])
            value_next = np.zeros([cf.num_D, ])
        elif (cf_L.MARL_type == 1):
            value_curr = 0
            value_next = 0

        # draw Poisson samples and generate a packet occurrence history
        pkt_occur, _ = NF.generate_pkt(cf.num_D, cf.num_S, cf_N.pkt_int, epi, Poisson_plot=False)

        for time_idx in range(cf.num_S):
            # packet arrival
            for a in range(cf.num_D):
                if pkt_occur[a, time_idx]:
                    if devices[a].queue == devices[a].Q_max:
                        devices[a].num_lost += 1
                    else:
                        devices[a].queue += 1

            if time_idx == 0: # initialize wireless channel
                for a in range(cf.num_D):
                    devices[a].beta = cf_N.r_loss*(cf_N.r_dist/devices[a].dist)**cf_N.path_exp
                    devices[a].snr_long = BF.LinearTodB((cf_N.t_pow*devices[a].beta)/(cf_N.BW*cf_N.n_pow))
                    NF.wireless_channel(devices[a], cf_N)

            # update channel use status
            ch_usage = NF.update_channel(devices, cf_N.SIFS, ch_usage)

            # update torch_device status
            NF.update_device(devices, ch_usage)

            # get state
            state = np.zeros([cf_L.dim_S,])
            for a in range(cf.num_D):
                state[a] = devices[a].since_success/60 # map 0~600 to 0~10
                # state[cf.num_D+a] = (devices[a].snr_long-20)/(40-20) # map 0~10 to 0~1
                state[cf.num_D+a] = devices[a].queue/(devices[a].Q_max) # map 0~10 to 0~1
            state[2*cf.num_D] = ch_usage

            # get status reward
            status_reward = np.zeros([cf.num_D,])
            for a in range(cf.num_D):
                if cf.objective == 0:
                    status_reward[a] += -1*devices[a].since_success/(15*cf.num_D) # map 0~600 to 0~-40/num_D
                elif cf.objective == 1:
                    tPut = (devices[a].tot_data*8/1e6) / ((time_idx+1)*cf_N.Ts) # Mbits/s
                    ratio = (tPut+0.5*cf.min_tPut)/(1.5*cf.min_tPut)
                    penalty = np.log(ratio**(8/cf.num_D)) if tPut < cf.min_tPut else 0
                    status_reward[a] += tPut/24 + penalty
                elif cf.objective == 2:
                    alpha = 12
                    tPut = (devices[a].tot_data*8/1e6) / ((time_idx+1)*cf_N.Ts) # Mbits/s
                    status_reward[a] += (tPut/(256/cf.num_D)+0.7)**(1-alpha)/(1-alpha)

            # store values
            States[epi].append(state)
            if args.debug == 1:
                Ch_uses[epi].append(ch_usage)
                for a in range(cf.num_D):
                    C_flags[epi][a].append(devices[a].cca_flag)
                    C_cnts[epi][a].append(devices[a].cca_counter)
                    B_flags[epi][a].append(devices[a].backoff_flag)
                    B_times[epi][a].append(devices[a].backoff_time)
                    B_cnts[epi][a].append(devices[a].backoff_counter)
                    T_flags[epi][a].append(devices[a].t_flag)
                    T_cnts[epi][a].append(devices[a].t_counter)
                    A_flags[epi][a].append(devices[a].ack_flag)
                    A_cnts[epi][a].append(devices[a].ack_counter)
                    Ques[epi][a].append(devices[a].queue)
                    E_times[epi][a].append(devices[a].since_success)

            # take action if needed
            for a in range(cf.num_D):
                if devices[a].cca_counter == devices[a].cca_len:
                    devices[a].cca_counter = 0

                    # update wireless channel
                    NF.wireless_channel(devices[a], cf_N)

                    if first_action[a] == 1:
                        NF.take_action(cf, cf_N, cf_L, devices[a], state, a,
                                       state_curr, obs_curr, action_curr, out_curr,
                                       logprob_curr, entropy_curr, value_curr,
                                       torch_device, args.debug, debug_hist)

                        if (devices[a].device_type == 0):
                            if (devices[a].ac_type == 1):
                                Out_Actor[epi][devices[a].type_id].append(out_curr[a].copy())
                                if (cf_L.MARL_type == 0):
                                    Out_Critic[epi][devices[a].type_id].append(value_curr[a].copy())
                                elif (cf_L.MARL_type == 1) & (a == cf.num_D-1):
                                    network_in = torch.as_tensor(np.ndarray.flatten(obs_curr), dtype=torch.float32).to(torch_device)
                                    value_curr = cent_critic(network_in).item()
                                    Out_Critic[epi].append(value_curr)

                        first_action[a] = 0

                    elif first_action[a] == 0:
                        reward = status_reward[a]

                        Rewards[epi][a].append(reward)
                        BF.store_sum(R_sums[epi][a], Rewards[epi][a][-1])

                        NF.take_action(cf, cf_N, cf_L, devices[a], state, a,
                                       state_next, obs_next, action_next, out_next,
                                       logprob_next, entropy_next, value_next,
                                       torch_device, args.debug, debug_hist)

                        if (devices[a].device_type == 0):
                            if (devices[a].ac_type == 1):
                                Out_Actor[epi][devices[a].type_id].append(out_next[a].copy())
                                if (cf_L.MARL_type == 0):
                                    Out_Critic[epi][devices[a].type_id].append(value_next[a].copy())
                                elif (cf_L.MARL_type == 1) & (a == cf.num_D-1):
                                    network_in = torch.as_tensor(np.ndarray.flatten(obs_next), dtype=torch.float32).to(torch_device)
                                    value_next = cent_critic(network_in).item()
                                    Out_Critic[epi].append(value_next)

                        if (devices[a].device_type == 0) & (save_buffer == 1):
                            devices[a].replay_buffer.add(obs_curr[a, :], out_curr[a], action_curr[a], reward,
                                obs_next[a, :], action_next[a], time_idx, state_curr[a, :], state_next[a, :], done[-1])

                        if devices[a].device_type == 0:
                            obs_curr_collect[a, devices[a].samples_collected, :] = torch.as_tensor(obs_curr[a, :])
                            obs_next_collect[a, devices[a].samples_collected, :] = torch.as_tensor(obs_next[a, :])
                            action_curr_collect[a, devices[a].samples_collected, 0] = torch.as_tensor(action_curr[a])
                            if (cf_L.MARL_type == 1):
                                reward = np.mean(status_reward)
                            reward_collect[a, devices[a].samples_collected, 0] = torch.as_tensor(reward)
                            done_collect[a, devices[a].samples_collected, 0] = torch.as_tensor(done[-1])

                            if devices[a].ac_type == 1:
                                logprob_collect[a, devices[a].samples_collected, 0] = torch.as_tensor(logprob_curr[a])
                                if (cf_L.MARL_type == 0):
                                    value_curr_collect[a, devices[a].samples_collected, 0] = torch.as_tensor(value_curr[a])
                                    value_next_collect[a, devices[a].samples_collected, 0] = torch.as_tensor(value_next[a])
                                elif (cf_L.MARL_type == 1) & (a == cf.num_D-1):
                                    value_curr_collect[devices[a].samples_collected, 0] = torch.as_tensor(value_curr)
                                    value_next_collect[devices[a].samples_collected, 0] = torch.as_tensor(value_next)

                            devices[a].samples_collected += 1
                            if devices[a].samples_collected == cf_L.num_B:
                                devices[a].update = 1
                                devices[a].samples_collected = 0

                        state_curr[a, :] = state_next[a, :].copy()
                        obs_curr[a, :] = obs_next[a, :].copy()
                        out_curr[a, :] = out_next[a, :].copy()
                        action_curr[a] = action_next[a].copy()
                        logprob_curr[a] = logprob_next[a].copy()
                        entropy_curr[a] = entropy_next[a].copy()
                        if (cf_L.MARL_type == 0):
                            value_curr[a] = value_next[a].copy()
                        elif (cf_L.MARL_type == 1) & (a == cf.num_D-1):
                            value_curr = value_next

                    Actions[epi][a].append(action_curr[a])
                    if action_curr[a] == 1:
                        devices[a].t_flag = 1
                    elif action_curr[a] == 0:
                        devices[a].t_flag = 0

                elif (devices[a].backoff_flag) & (devices[a].backoff_counter == devices[a].backoff_time):
                    devices[a].backoff_flag = 0
                    devices[a].backoff_time = 0
                    devices[a].backoff_counter = 0

                    if devices[a].device_type == 2:
                        Actions[epi][a].append(1)
                        devices[a].t_flag = 1
                    elif devices[a].device_type == 3:
                        queue_prob = np.exp(devices[a].queue*cf_N.Q_alpha)/(np.exp(devices[a].queue*cf_N.Q_alpha)+1)
                        if np.random.binomial(1, queue_prob):
                            Actions[epi][a].append(1)
                            devices[a].t_flag = 1
                else:
                    Actions[epi][a].append(0.5)

            tmp = 0
            for a in ids_agent:
                tmp += devices[a].update

            if (tmp == num_A) & (num_A != 0):
                update_start_time = time.time()
                if cf_L.AC_type == 0:
                    for a in range(cf_L.num_B):
                        for _ in range(cf.num_G):
                            reward_collect[:, a, :] = A_consensus @ reward_collect[:, a, :]

                    for a in ids_agent:
                        assert (devices[a].device_type == 0) & (devices[a].update)
                        outputs = LF.agent_update(devices[a], a, obs_curr_collect[a], obs_next_collect[a],
                                                  action_curr_collect[a], reward_collect[a], done_collect[a], cf_L)
                        Out_Critic[epi][a].append(outputs[0])
                        Out_TD[epi][a].append(outputs[1])
                        Out_Actor[epi][a].append(outputs[2])
                        critic_grad_norms[a].append(outputs[3])
                        actor_grad_norms[a].append(outputs[4])
                        # debug_loss[a].append(outputs[5])
                        # debug_norm[a].append(outputs[6])
                        devices[a].update = 0

                elif cf_L.AC_type == 1:
                    advantages, returns, deltas = LF.compute_gae(reward_collect, value_curr_collect, value_next_collect, done_collect,
                                   args_dppo.gamma, args_dppo.gae_lambda, cf_L)
                    for a in ids_agent:
                        Out_TD[epi][a].append(deltas[a].numpy())

                    pg_losses = {a: [] for a in range(num_A)}
                    v_losses = {a: [] for a in range(num_A)}
                    entropy_losses = {a: [] for a in range(num_A)}
                    approx_kls = {a: [] for a in range(num_A)}
                    clipfracs = {a: [] for a in range(num_A)}

                    indices = torch.randperm(cf_L.num_B, device=torch_device)
                    minibatch_size = cf_L.num_B // cf_L.num_minibatches
                    for start in range(0, cf_L.num_B, minibatch_size):
                        end = min(start + minibatch_size, cf_L.num_B)
                        mb_indices = indices[start:end]

                        # Prepare data for ALL agents simultaneously
                        all_losses = []

                        # STEP 1: Store network outputs for each agent
                        agent_outputs = []

                        if (cf_L.MARL_type == 1):
                            mb_obs_cent = obs_curr_collect[:, mb_indices, :].permute(1,0,2).flatten(start_dim=1)

                        for a in range(num_A):
                            mb_obs = obs_curr_collect[a, mb_indices, :]
                            mb_actions = action_curr_collect[a, mb_indices, 0]
                            mb_logprobs = logprob_collect[a, mb_indices, 0]
                            mb_advantages = advantages[a, mb_indices, 0]

                            # Compute new policy values
                            _, new_logprob, entropy, _ = devices[a].actor(mb_obs, mb_actions)
                            if (cf_L.MARL_type == 0):
                                new_value = devices[a].critic(mb_obs)
                            elif (cf_L.MARL_type == 1):
                                new_value = cent_critic(mb_obs_cent)

                            ratio = torch.exp(new_logprob - mb_logprobs)

                            # Store all outputs for this agent
                            agent_outputs.append({
                                'ratio': ratio, # each individual agent's ratios
                                'advantage': mb_advantages,
                                'new_logprob': new_logprob,
                                'entropy': entropy,
                                'new_value': new_value
                            })

                        for a in range(num_A):
                            # STEP 2: Compute average advantage (no gradient flow needed)
                            if args_dppo.agent_com:
                                avg_advantage = torch.stack([out['advantage'] for out in agent_outputs], dim=0).mean(dim=0)
                            else:
                                avg_advantage = agent_outputs[a]['advantage']

                            # Normalize averaged advantages
                            if args_dppo.norm_adv and avg_advantage.std() > 1e-8:
                                avg_advantage = (avg_advantage - avg_advantage.mean()) / (avg_advantage.std() + 1e-8)

                            # STEP 3: Compute losses and update each agent independently
                            # Build ratio product with THIS agent's ratio having gradients,
                            # but OTHER agents' ratios detached (no gradient flow)
                            ratio_components = []
                            for other_idx in range(num_A):
                                if other_idx == a:
                                    # Keep gradients for current agent
                                    ratio_components.append(agent_outputs[other_idx]['ratio'])
                                else:
                                    # Detach other agents' ratios to prevent gradient flow
                                    ratio_components.append(agent_outputs[other_idx]['ratio'].detach())

                            # Product of ratios (only current agent has gradients)
                            if args_dppo.agent_com:
                                ratio_product = torch.stack(ratio_components, dim=0).prod(dim=0)
                            else:
                                ratio_product = agent_outputs[a]['ratio']

                            # Get this agent's outputs
                            new_logprob = agent_outputs[a]['new_logprob']
                            entropy = agent_outputs[a]['entropy']
                            new_value = agent_outputs[a]['new_value']

                            # Prepare returns and old values for this agent
                            mb_returns = returns[a, mb_indices, 0]
                            if (cf_L.MARL_type == 0):
                                mb_values = value_curr_collect[a, mb_indices, 0]
                            elif (cf_L.MARL_type == 1):
                                mb_values = value_curr_collect[mb_indices, 0]
                            mb_logprobs = logprob_collect[a, mb_indices, 0]

                            # Policy loss using ratio product (only THIS agent's gradients flow)
                            pg_loss1 = -avg_advantage * ratio_product
                            pg_loss2 = -avg_advantage * torch.clamp(
                                ratio_product,
                                1 - args_dppo.clip_coef,
                                1 + args_dppo.clip_coef
                            )
                            pg_loss = torch.max(pg_loss1, pg_loss2).mean()

                            # Value loss (agent-specific)
                            if args_dppo.clip_vloss:
                                v_loss_unclipped = (new_value - mb_returns) ** 2
                                v_clipped = mb_values + torch.clamp(
                                    new_value - mb_values,
                                    -args_dppo.clip_coef,
                                    args_dppo.clip_coef
                                )
                                v_loss_clipped = (v_clipped - mb_returns) ** 2
                                v_loss = 0.5 * torch.max(v_loss_unclipped, v_loss_clipped).mean()
                            else:
                                v_loss = 0.5 * ((new_value - mb_returns) ** 2).mean()

                            entropy_loss = entropy.mean()

                            # Total loss for this agent
                            loss = pg_loss - args_dppo.ent_coef * entropy_loss + args_dppo.vf_coef * v_loss
                            all_losses.append(loss)

                            # Track metrics PER AGENT (using local ratio for monitoring)
                            with torch.no_grad():
                                logratio_local = new_logprob - mb_logprobs
                                ratio_local = logratio_local.exp()
                                approx_kl = ((ratio_local - 1) - logratio_local).mean()
                                clipfrac = ((ratio_local - 1.0).abs() > args_dppo.clip_coef).float().mean()

                                # Append to this agent's specific lists
                                pg_losses[a].append(pg_loss.item())
                                v_losses[a].append(v_loss.item())
                                entropy_losses[a].append(entropy_loss.item())
                                approx_kls[a].append(approx_kl.item())
                                clipfracs[a].append(clipfrac.item())

                        # STEP 4: SIMULTANEOUS gradient update for all agents
                        # Zero gradients for all optimizers
                        for a in range(num_A):
                            devices[a].actor_optimizer.zero_grad()
                            if (cf_L.MARL_type == 0):
                                devices[a].critic_optimizer.zero_grad()
                        if (cf_L.MARL_type == 1):
                            cent_critic_optimizer.zero_grad()

                        # Compute gradients for all agents (but don't step yet)
                        for a in range(num_A):
                            all_losses[a].backward()

                        # Clip gradients and step all optimizers simultaneously
                        for a in range(num_A):
                            actor_grad_norms[a].append(torch.nn.utils.clip_grad_norm_(devices[a].actor.parameters(), cf_L.actor_clip))
                            devices[a].actor_optimizer.step()
                            if (cf_L.MARL_type == 0):
                                critic_grad_norms[a].append(torch.nn.utils.clip_grad_norm_(devices[a].critic.parameters(), cf_L.critic_clip))
                                devices[a].critic_optimizer.step()

                            devices[a].update = 0

                        if (cf_L.MARL_type == 1):
                            critic_grad_norms.append(torch.nn.utils.clip_grad_norm_(cent_critic.parameters(), cf_L.critic_clip))
                            cent_critic_optimizer.step()

                update_end_time = time.time()
                Update_times[epi].append(update_end_time - update_start_time)

            # check collision
            flag_sum = 0
            for a in range(cf.num_D):
                flag_sum += devices[a].t_flag

            for a in range(cf.num_D):
                if (devices[a].t_flag) & (devices[a].t_counter == devices[a].t_len):
                    ch_usage = 0
                    devices[a].t_flag = 0
                    devices[a].t_counter = 0

                    if flag_sum > 1: # if collision
                        devices[a].ack_flag = 0
                        devices[a].num_collision += 1
                        devices[a].tot_energy += devices[a].energy

                        if (cf_N.BEB) & (devices[a].CW < cf_N.CW_max):
                            devices[a].CW = devices[a].CW * 2
                    else: # if no collision
                        devices[a].ack_flag = 1

                if (devices[a].ack_flag) & (devices[a].ack_counter == devices[a].ack_len):
                    ch_usage = 0
                    devices[a].ack_flag = 0
                    devices[a].ack_counter = 0

                    devices[a].queue += -1
                    devices[a].num_success += 1
                    dels[epi][a].append(devices[a].since_success)
                    datas[epi][a].append(devices[a].data/8) # bytes
                    devices[a].tot_data += devices[a].data/8 # bytes
                    devices[a].tot_energy += devices[a].energy
                    devices[a].since_success = 0

            # time elapses for all devices
            for a in range(cf.num_D):
                devices[a].since_success += 1

            if (time_idx+1) % cf.num_S == 0:
                done.append(1)
            else:
                done.append(0)

        for a in range(cf.num_D):
            Pkt_tot[rnd][a].append(np.sum(pkt_occur[a,:]))
            Pkt_coll[rnd][a].append(devices[a].num_collision)
            Pkt_suc[rnd][a].append(devices[a].num_success)
            if len(dels[epi][a]) == 0:
                Pkt_del[rnd][a].append(cf.num_S)
            else:
                Pkt_del[rnd][a].append(np.mean(dels[epi][a]))
            R_avgs[rnd][a].append(R_sums[epi][a][-1]/len(R_sums[epi][a]))
            Tputs[rnd][a].append(devices[a].tot_data*8/(cf.num_S*cf_N.Ts)) # bits/s
        Update_Ts[rnd].append(sum(Update_times[epi]))

        epi_end_time = time.time()
        Total_Ts[rnd].append(epi_end_time - epi_start_time)

    if save_buffer:
        for a in ids_agent:
            devices[a].replay_buffer.save(work_dir, devices[a].device_id, cf, cf_L, rnd)

    print(f"Elapsed time: {sum(Total_Ts[rnd]):.3f} seconds")

if save_file:
    items_to_save = [cf, cf_N, cf_L, args_dppo, Pkt_tot, Pkt_coll, Pkt_suc, Pkt_del, R_avgs,
                     Actions, Rewards, Out_Critic, Out_Actor, Out_TD, Tputs, Update_Ts, Total_Ts]

    with open(f'{file_name}.pkl', 'wb') as file:
        pickle.dump(items_to_save, file)

#%% Summary
print(cf)
print(cf_N)
print(cf_L)
print("---------------------------------")
print(f"{file_name}")

for a in range(num_A):
    print(f"Device {a+1} Actor Norm [> {cf_L.actor_clip} = {np.sum(np.array(actor_grad_norms[a]) > cf_L.actor_clip)}] \
[> {0.5*cf_L.actor_clip} = {np.sum(np.array(actor_grad_norms[a]) > 0.5*cf_L.actor_clip)}] out of {len(actor_grad_norms[a])}")
    if (cf_L.MARL_type == 0) | (cf_L.MARL_type == 3) | (cf_L.MARL_type == 4):
        print(f"Device {a+1} Critic Norm [> {cf_L.critic_clip} = {np.sum(np.array(critic_grad_norms[a]) > cf_L.critic_clip)}] \
[> {0.5*cf_L.critic_clip} = {np.sum(np.array(critic_grad_norms[a]) > 0.5*cf_L.critic_clip)}] out of {len(critic_grad_norms[a])}")
if (cf_L.MARL_type == 1) | (cf_L.MARL_type == 2):
    print(f"Central Critic Norm [> {cf_L.critic_clip} = {np.sum(np.array(critic_grad_norms) > cf_L.critic_clip)}] \
[> {0.5*cf_L.critic_clip} = {np.sum(np.array(critic_grad_norms) > 0.5*cf_L.critic_clip)}] out of {len(critic_grad_norms)}")

# print(f"State mean: {np.mean(np.array(States), axis=(0,1))}")
# print(f"State std: {np.std(np.array(States), axis=(0,1))}")

num_E_to_avg = 20

tmp, arr = BF.organize_arr(Pkt_coll, cf.num_E, cf.num_R, 2)
if (len(ids_agent) != cf.num_D) & (len(ids_agent) != 0):
    tmpp = tmp[ids_agent, -num_E_to_avg:]
    print(f'Collided pkts (Agent) = mean: {np.mean(tmpp):.2f}')
if (len(ids_legacy) != cf.num_D) & (len(ids_legacy) != 0):
    tmpp = tmp[ids_legacy, -num_E_to_avg:]
    print(f'Collided pkts (Legacy) = mean: {np.mean(tmpp):.2f}')
tmpp = tmp[:, -num_E_to_avg:]
print(f'Collided pkts (Total) = mean: {np.mean(tmpp):.2f}')

tmp, arr = BF.organize_arr(np.array(Pkt_suc)*cf_N.pkt_len*8/(cf.num_S*cf_N.Ts)/1e6, cf.num_E, cf.num_R, 2)
if (len(ids_agent) != cf.num_D) & (len(ids_agent) != 0):
    tmpp = tmp[ids_agent, -num_E_to_avg:]
    arrr = arr[:, ids_agent, -num_E_to_avg:]
    print(f"tPut (Agent) = sum: {np.sum(tmpp)/num_E_to_avg:.3f} +/- {np.std(np.sum(arrr, axis=(1,2))/num_E_to_avg):.3f}, \
mean: {np.mean(tmpp):.3f}, min: {np.min(np.mean(tmpp, axis=1)):.3f}, max: {np.max(np.mean(tmpp, axis=1)):.3f} Mbps")
if (len(ids_legacy) != cf.num_D) & (len(ids_legacy) != 0):
    tmpp = tmp[ids_legacy, -num_E_to_avg:]
    arrr = arr[:, ids_legacy, -num_E_to_avg:]
    print(f"tPut (Legacy) = sum: {np.sum(tmpp)/num_E_to_avg:.3f} +/- {np.std(np.mean(arrr, axis=(1,2))/num_E_to_avg):.3f}, \
mean: {np.mean(tmpp):.3f}, min: {np.min(np.mean(tmpp, axis=1)):.3f}, max: {np.max(np.mean(tmpp, axis=1)):.3f} Mbps")
tmpp = tmp[:, -num_E_to_avg:]
arrr = arr[:, :, -num_E_to_avg:]
print(f"tPut (Total) = sum: {np.sum(tmpp)/num_E_to_avg:.3f} Mbps +/- {np.std(np.mean(arrr, axis=(1,2))/num_E_to_avg):.3f}, \
mean: {np.mean(tmpp):.3f}, min: {np.min(np.mean(tmpp, axis=1)):.3f}, max: {np.max(np.mean(tmpp, axis=1)):.3f}, \
N-Gap: {(np.max(np.mean(tmpp, axis=1))-np.min(np.mean(tmpp, axis=1)))/np.max(np.mean(tmpp, axis=1)):.3f}")

tmp, arr = BF.organize_arr(np.array(Pkt_del)*cf_N.Ts*1000, cf.num_E, cf.num_R, 2)
if (len(ids_agent) != cf.num_D) & (len(ids_agent) != 0):
    tmpp = tmp[ids_agent, -num_E_to_avg:]
    arrr = arr[:, ids_agent, -num_E_to_avg:]
    print(f'AoI mean (Agent) = mean: {np.mean(tmpp):.3f} +/- {np.std(np.mean(arrr, axis=(1,2))):.3f} ms, \
max: {np.max(np.mean(tmpp, axis=1)):.3f}, min: {np.min(np.mean(tmpp, axis=1)):.3f}')
if (len(ids_legacy) != cf.num_D) & (len(ids_legacy) != 0):
    tmpp = tmp[ids_legacy, -num_E_to_avg:]
    arrr = arr[:, ids_legacy, -num_E_to_avg:]
    print(f'AoI mean (Legacy) = mean: {np.mean(tmpp):.3f} +/- {np.std(np.mean(arrr, axis=(1,2))):.3f} ms, \
max: {np.max(np.mean(tmpp, axis=1)):.3f}, min: {np.min(np.mean(tmpp, axis=1)):.3f}')
tmpp = tmp[:, -num_E_to_avg:]
arrr = arr[:, :, -num_E_to_avg:]
print(f'AoI mean (Total) = mean: {np.mean(tmpp):.3f} +/- {np.std(np.mean(arrr, axis=(1,2))):.3f} ms, \
max: {np.max(np.mean(tmpp, axis=1)):.3f}, min: {np.min(np.mean(tmpp, axis=1)):.3f}, N-Gap: {(np.max(np.mean(tmpp, axis=1))-np.min(np.mean(tmpp, axis=1)))/np.max(np.mean(tmpp, axis=1)):.3f}')

Total_T_mean = np.mean(np.array(Total_Ts), axis=0)
Total_T_sum = []
for a in Total_T_mean:
    BF.store_sum(Total_T_sum, a)

Update_T_mean = np.mean(np.array(Update_Ts), axis=0)
Update_T_sum = []
for a in Update_T_mean:
    BF.store_sum(Update_T_sum, a)
print(f"Average Runtime = Update {Update_T_sum[-1]:.3f} / Total {Total_T_sum[-1]:.3f} sec")