#%% Import packages -----------------------------------------------------------
import numpy as np
import torch
import networkx as nx
import marl_base as BF
import marl_learning as LF

#%% defines -------------------------------------------------------------------

class RA_device:
    def __init__(self, device_id, device_type, type_id, cf, cf_N, cf_L, torch_device, initial_dist=1):
        self.device_id = device_id
        self.device_type = device_type
        self.type_id = type_id # id within the type
        self.Q_max = cf_N.Q_max
        self.cca_len = cf_N.DIFS
        self.t_len = cf_N.t_len
        self.ack_len = cf_N.SIFS + cf_N.ACK
        self.energy = cf_N.t_pow*cf_N.Ts*self.t_len

        self.CW = cf_N.CW_min
        self.cca_flag = 0
        self.cca_counter = 0
        self.backoff_flag = 0
        self.backoff_time = 0
        self.backoff_counter = 0
        self.t_flag = 0
        self.t_counter = 0
        self.ack_flag = 0
        self.ack_counter = 0

        self.queue = 0
        self.since_success = 0
        self.num_success = 0
        self.num_collision = 0
        self.num_lost = 0

        self.dist = initial_dist
        self.beta = 0
        self.snr_long = 0
        self.h_ch = np.zeros([cf_N.num_Rx,])
        self.g_ch = np.zeros([cf_N.num_Rx,])
        self.snr_short = 0
        self.cap = 0
        self.data = 0
        self.tot_data = 1e-2
        self.tot_energy = 1e-10

        if self.device_type == 0:
            self.net_type = cf_L.net_type
            self.ac_type = cf_L.AC_type
            self.dim_W = cf_L.dim_W
            self.dim_D = cf_L.dim_D
            self.dim_O = cf_L.dim_O
            self.dim_A = cf_L.dim_A
            self.dim_H = cf_L.dim_H

            self.samples_collected = 0
            self.update = 0

            if self.ac_type == 0: # A2C
                self.actor = LF.Actor_A2C(self.dim_H + self.dim_O, self.dim_W, self.dim_D, self.dim_A, self.net_type, self.ac_type, cf_L).to(torch_device)
                self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=cf_L.actor_rate, eps=1e-5)
                if (cf_L.MARL_type == 0):
                    self.critic = LF.Critic(self.dim_H + self.dim_O, self.dim_W, self.dim_D, self.net_type, cf_L).to(torch_device)
                    self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=cf_L.critic_rate, eps=1e-5)

            elif self.ac_type == 1: # PPO
                self.actor = LF.Actor_PPO(self.dim_H + self.dim_O, self.dim_W, self.dim_D, self.dim_A, self.net_type, self.ac_type, cf_L).to(torch_device)
                self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=cf_L.actor_rate, eps=1e-5)
                if (cf_L.MARL_type == 0):
                    self.critic = LF.Critic(self.dim_H + self.dim_O, self.dim_W, self.dim_D, self.net_type, cf_L).to(torch_device)
                    self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=cf_L.critic_rate, eps=1e-5)

            self.replay_buffer = LF.ReplayBuffer(
                obs_shape = (self.dim_O,),
                output_shape = (1,),
                action_shape = (1,),
                state_shape = (cf_L.dim_S,),
                capacity = cf_L.buffer_capacity,
                batch_size = 128,
                dim_O = self.dim_O,
                dim_A = self.dim_A,
                dim_H = self.dim_H,
                device = torch_device)

    def reset(self, cf_N, initial_dist):
        self.CW = cf_N.CW_min
        self.cca_flag = 0
        self.cca_counter = 0
        self.backoff_flag = 0
        self.backoff_time = 0
        self.backoff_counter = 0
        self.t_flag = 0
        self.t_counter = 0
        self.ack_flag = 0
        self.ack_counter = 0

        self.queue = 1
        self.since_success = 0
        self.num_success = 0
        self.num_collision = 0
        self.num_lost = 0

        self.dist = initial_dist
        self.beta = 0
        self.snr_long = 0
        self.h_ch = np.zeros([cf_N.num_Rx,])
        self.g_ch = np.zeros([cf_N.num_Rx,])
        self.snr_short = 0
        self.cap = 0
        self.data = 0
        self.tot_data = 1e-2
        self.tot_energy = 1e-10

        if self.device_type == 0:
            self.samples_collected = 0
            self.update = 0

def watts_strogatz(N, K, beta):
    """
    Returns a Watts-Strogatz model graph with N nodes, N*K edges,
    mean node degree 2*K, and rewiring probability beta.

    Parameters:
    - N (int): Number of nodes
    - K (int): Each node is connected to K nearest neighbors in ring topology
    - beta (float): The probability of rewiring each edge

    Returns:
    - G (networkx.Graph): A Watts-Strogatz small-world graph
    """

    # Initialize a ring lattice with N nodes, each connected to K neighbors
    s = np.repeat(np.arange(N), K)
    t = (s.reshape(N, K) + np.arange(1, K + 1)) % N
    t = t.flatten()

    # Initialize the graph with these edges
    G = nx.Graph()
    G.add_edges_from(zip(s, t))

    # Rewire edges with probability beta
    for source in range(N):
        # Determine which edges to rewire
        switch_edge = np.random.rand(K) < beta

        # Generate random new targets and exclude invalid choices
        new_targets = np.random.rand(N)
        new_targets[source] = 0  # Prevent self-loop
        new_targets[t[s == source]] = 0  # Prevent duplicate edges

        # Sort potential new targets in descending order and select replacements
        ind = np.argsort(-new_targets)  # Descending sort
        t[s == source][switch_edge] = ind[:np.sum(switch_edge)]

    # Update graph with rewired edges
    G = nx.Graph()
    G.add_edges_from(zip(s, t))

    return G

def turn_graph_to_matrix(G):
    """
    Converts a NetworkX graph to an adjacency matrix.

    Parameters:
    - G (networkx.Graph): The input graph.

    Returns:
    - C (numpy.ndarray): The adjacency matrix of the graph.
    - N (int): The number of nodes in the graph.
    """

    edges = np.array(G.edges)
    N = max(max(edges.flatten()), G.number_of_nodes())
    C = np.eye(N, dtype=int)

    for edge in edges:
        m, n = edge
        # C[m - 1, n - 1] = 1
        # C[n - 1, m - 1] = 1
        C[m, n] = 1
        C[n, m] = 1

    return C, N

def consensus(num_A, num_N, num_G, beta=0, shuffle=False):
    G = watts_strogatz(N=num_A, K=num_N, beta=beta) # Create Graph
    A_consensus = turn_graph_to_matrix(G)[0] # Get corresponding matrix
    row_sums = A_consensus.sum(axis=1)
    A_consensus = A_consensus / row_sums[:, np.newaxis]
    if shuffle:
        A_consensus = BF.permute_symmetric_off_diagonal(A_consensus)
    print("Consensus Matrix")
    print(A_consensus)
    print("After Gossiping")
    print(np.linalg.matrix_power(A_consensus, num_G))

    return A_consensus

def generate_pkt(num_D, num_S, pkt_int, epi, Poisson_plot=False):
    pkt_occur = np.zeros([num_D, num_S])
    pkt_occur_sum = np.zeros([num_D, num_S])

    for a in range(num_D):
        # draw enough to cover the entire episode
        poisson_sample = np.random.poisson(pkt_int, ((num_S + 1000) // pkt_int, ))
        sample_idx = 0
        step_idx = poisson_sample[sample_idx]
        while step_idx < num_S:
            sample_idx += 1
            pkt_occur[a, step_idx] = 1
            pkt_occur_sum[a, step_idx:] = sample_idx
            step_idx += poisson_sample[sample_idx]

    # if (Poisson_plot) & (epi == 0):
    #     _, axes = plt.subplots(4, 1)
    #     for a, ax in enumerate(axes):
    #         ax.stem(pkt_occur[a,:])
    #         ax.set_xlabel('Time Slot Index')
    #         ax.set_ylabel(f"Device {a}")
    #     plt.show()
    #     plt.tight_layout()

    #     plt.figure()
    #     plt.plot(pkt_occur_sum.T)
    #     plt.xlabel('Time Slot Index')
    #     plt.ylabel('Total Packets Arrived')
    #     legend_str = ['$\\lambda$ = %.3f' %(1/pkt_int) for a in range(num_D)]
    #     plt.legend(legend_str)
    #     plt.grid()
    #     plt.tight_layout()

    return pkt_occur, pkt_occur_sum

def update_channel(devices, SIFS, ch_usage):
    for a in range(len(devices)):
        if devices[a].t_flag == 1: # device is transmitting pkt
            assert (devices[a].ack_flag == 0) & (devices[a].ack_counter == 0)
            devices[a].t_counter += 1
            ch_usage = 1
        elif devices[a].ack_flag == 1: # device is expected to receive ACK
            assert (devices[a].t_flag == 0) & (devices[a].t_counter == 0)
            devices[a].ack_counter += 1
            if devices[a].ack_counter > SIFS:
                ch_usage = 1
    return ch_usage

def update_device(devices, ch_usage):
    for a in range(len(devices)):
        if ch_usage: # everything is off if channel is busy
            devices[a].ch_never_used = 0
            devices[a].cca_flag = 0
            devices[a].cca_counter = 0
            devices[a].backoff_flag = 0
            devices[a].backoff_counter = 0
        elif devices[a].backoff_flag: # continue backoff if channel is idle
            devices[a].cca_flag = 0
            devices[a].cca_counter = 0
            devices[a].backoff_counter += 1
        elif devices[a].queue > 0: # enter CCA if channel is idle, device not in backoff, and buffer has any queue
            devices[a].cca_flag = 1
            devices[a].cca_counter += 1

def wireless_channel(device, cf_N):
    device.h_ch = BF.get_Gaussian(mean=0.0, var=1.0, size=(cf_N.num_Rx,), sig_type='complex')
    # device.beta = cf_N.r_loss*(cf_N.r_dist/device.dist)**cf_N.path_exp
    device.g_ch = np.sqrt(device.beta)*device.h_ch
    dev_snr = cf_N.t_pow*np.sum(np.abs(device.g_ch)**2)/(cf_N.BW*cf_N.n_pow)
    device.snr_short = BF.LinearTodB(dev_snr)
    device.cap = cf_N.BW*np.log2(1+dev_snr)
    device.data = device.cap*cf_N.Ts*cf_N.t_len

def take_action(cf, cf_N, cf_L, device, state, a,
                state_arr, obs_arr, action_arr, out_arr,
                logprob_arr, entropy_arr, value_arr,
                torch_device, debug_mode, debug_hist):
    state_arr[a, :] = state.copy()

    obs_arr[a, 0:cf.num_D] = state[0:cf.num_D] # delay
    obs_arr[a, cf.num_D] = state[cf.num_D + a] # queue
    obs_arr[a, -1] = state[-1] # ch usage

    if device.device_type == 0:
        network_in = torch.as_tensor(obs_arr[a, :], dtype=torch.float32).to(torch_device)

        if (cf_L.AC_type == 0):
            out_arr[a, :] = device.actor(network_in).cpu().tolist()

            if cf_L.action_type == 0: # discrete action
                action_arr[a] = np.random.choice(np.shape(out_arr)[1], p=out_arr[a, :]/np.sum(out_arr[a, :]))
            elif cf_L.action_type == 1: # continuous action
                action_arr[a] = np.random.binomial(1, out_arr[a, :])

            if out_arr[a, action_arr[a]] < 1e-5:
                print(f"Actual {a}, {action_arr[a]}, {out_arr[action_arr[a]]}")

            if debug_mode == 1:
                debug_hist[a].append([network_in.cpu().tolist(), out_arr[a, :], action_arr[a].copy()])

        elif (cf_L.AC_type == 1):
            action_arr[a], logprob_arr[a], entropy_arr[a], out_arr[a, :] = device.actor(network_in)
            if (cf_L.MARL_type == 0) | (cf_L.MARL_type == 4):
                value_arr[a] = device.critic(network_in)

            if logprob_arr[a] < -7:
                if cf_L.MARL_type == 1:
                    print(f"Actual {a}, {action_arr[a]}, {out_arr[action_arr[a]]}, {logprob_arr[a]}, {entropy_arr[a]}")
                else:
                    print(f"Actual {a}, {action_arr[a]}, {out_arr[action_arr[a]]}, {logprob_arr[a]}, {entropy_arr[a]}, {value_arr[a]}")

            if debug_mode == 1:
                debug_hist[a].append([network_in.cpu().tolist(), out_arr[a, :], action_arr[a].copy(), logprob_arr[a].copy(), entropy_arr[a].copy(), value_arr[a].copy()])

    elif device.device_type == 1:
        action_arr[a] = np.random.binomial(1, cf_N.transmit_prob)

    elif device.device_type == 2:
        device.backoff_time = np.random.randint(0, device.CW)
        if device.backoff_counter == device.backoff_time:
            action_arr[a] = 1
        else:
            action_arr[a] = 0
            device.backoff_flag = 1

    elif device.device_type == 3:
        device.backoff_time = np.random.randint(0, device.CW)
        if device.backoff_counter == device.backoff_time:
            queue_prob = np.exp(device.queue*cf_N.Q_alpha)/(np.exp(device.queue*cf_N.Q_alpha)+1)
            action_arr[a] = np.random.binomial(1, queue_prob)
        else:
            action_arr[a] = 0
            device.backoff_flag = 1