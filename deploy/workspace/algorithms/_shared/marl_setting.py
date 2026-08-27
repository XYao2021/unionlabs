"""
Every knob a MARL run takes, as dataclasses rather than loose arguments.

    SimSettings      how long, how many episodes, what to log
    NetworkSettings  how many devices, how they are connected, traffic
    LearningSettings the optimiser, the network shape, the reward
    DPPO_Args        the PPO-specific hyper-parameters

Grouping them this way means a run is described by data that can be printed,
diffed and saved with its results -- which is what makes an experiment
reproducible after the fact, when the command line that produced it is gone.
"""
#%% Import packages -----------------------------------------------------------
from typing import Optional
import dataclasses
import marl_base as BF

#%% defines -------------------------------------------------------------------

@dataclasses.dataclass
class SimSettings:
    device_type: int = -99 # 0 for MARL, 1 for q-ALOHA, 2 for CSMA/CA, 3 for Q-CSMA
    objective: int = -99 # 0 for fair AoI, 1 for max rate, 2 for fair rate
    num_D: int = -99 # num. of devices
    num_R: int = -99 # num. of simulation runs
    num_E: int = -99 # num. of episodes per run
    num_S: int = 600 # num. of time steps per episode

    num_N: int = -99 # num. of neighbors in edge connection
    num_G: int = 1 # num. of gossiping
    update_ratio: float = 2 # edge connection topology update ratio (set 2 for "no update")
    update_int: int = -99 # edge connection topology update interval (unit: time slots)
    conn_prob: float = 1 # edge connection probability

    fixed_seed: int = 0 # 0 = random seed, 1 = use "seed idx"
    seed_idx: int = 50

@dataclasses.dataclass
class NetworkSettings:
    transmit_prob: float = -99 # transmit probability for ALOHA

    Q_max: int = 50 # buffer size
    pkt_int: int = 10 # packet arrival interval for each device (unit: time slots per packet)
    pkt_len: int = 1500 # packet size (unit: bytes)
    Ts: float = 9e-6 # [9 us] time slot length (unit: sec)
    t_len: int = 10 # packet transmission duration (unit: time slots)
    SIFS: int = 2 # (unit: time slots)
    DIFS: int = 4 # (unit: time slots)
    ACK: int = 4 # (unit: time slots)

    BEB: int = 1 # 0 for fixed CW, 1 for BEB
    CW_min: int = 1 # starting CW size
    CW_max: int = 1024 # maximum CW size
    Q_alpha: float = 1 # for Q-CSMA

    num_Rx: int = 2 # num. of Rx antennas
    BW: int = 10e6 # UL bandwidth (10 MHz)
    t_pow: float = 0.001*BF.dBToLinear(0) # transmit power in linear (0 dBm)
    n_pow: float = 0.001*BF.dBToLinear(-174) # noise spectral density in linear (-174 dBm/Hz)
    r_dist: float = 1 # reference distance (1 m)
    r_loss: float = BF.dBToLinear(-40) # reference power in linear (-40 dB)
    path_exp: int = 4 # pathloss exponent

@dataclasses.dataclass
class LearningSettings:
    MARL_type: int = -99 # 0 for DTDE, 1 for CTDE
    AC_type: int = -99  # 0 for A2C, 1 for PPO
    net_type: int = 0 # 0 for MLP, 1 for ResNet

    dim_W: int = 64 # model width
    dim_D: int = 2 # model depth
    actor_rate: float = -99 # actor update rate
    critic_rate: float = -99 # critic update rate
    actor_clip: int = 10 # gradient clipping threshold for actor (norm)
    critic_clip: int = 10 # gradient clipping threshold for critic (norm)

    M: int = 0 # History length
    dim_S: int = -99 # state dimension
    dim_O: int = -99 # observation dimension
    dim_H: int = -99 # history dimension

    GAMMA: float = 0.9 # future discount factor
    action_type: int = 0 # 0 = discrete, 1 = continuous
    dim_A = 2 # 1 for continuous action space dimension [transmit prob],
              # 2 for discrete action space dimension [no, yes]

    buffer_capacity: int = -99
    num_B: int = 32 # samples to collect prior to update
    num_minibatches: int = 4 # num. of minibatches to subdivide the batch

@dataclasses.dataclass
class DPPO_Args:
    seed: int = 3
    total_timesteps: int = 15_000_000
    learning_rate: float = 7e-4 # 3e-4
    num_envs: int = 4
    num_steps: int = 128
    num_minibatches: int = 4
    update_epochs: int = 4
    gamma: float = 0.9
    gae_lambda: float = 0.9
    clip_coef: float = 0.2
    ent_coef: float = 0.01
    vf_coef: float = 0.5
    max_grad_norm: float = .5  # what should be a good value
    anneal_lr: bool = True
    norm_adv: bool = True
    clip_vloss: bool = True
    agent_com: bool = True
    max_cycles: int = 25
    local_ratio: float = 0.5  # Weight for local rewards for heterogeneous agents

    # Logging and checkpointing
    log_interval: int = 10 # for both tensorboard and terminal
    eval_interval: int = 50
    eval_episodes: int = 100
    save_interval: int = 10_000 # can be further refined
    save_best_model: bool = True
    # TensorBoard
    track: bool = True

    # Directories 
    run_id: Optional[str] = None  # Set at runtime
    runs_dir: str = "runs"  # Base directory for all runs

    # def __post_init__(self):
    #     """Generate run-specific directories after initialization."""
    #     if self.run_id is None:
    #         # Generate run ID from timestamp and key hyperparameters
    #         timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    #         self.run_id = f"{timestamp}_seed{self.seed}"

    # @property
    # def run_dir(self) -> str:
    #     """Full path to this run's directory."""
    #     return os.path.join(self.runs_dir, self.exp_name, self.run_id)

    # @property
    # def checkpoint_dir(self) -> str:
    #     """Directory for model checkpoints."""
    #     return os.path.join(self.run_dir, "checkpoints")

    # @property
    # def tensorboard_dir(self) -> str:
    #     """Directory for TensorBoard logs."""
    #     return os.path.join(self.run_dir, "tensorboard")

    # @property
    # def config_path(self) -> str:
    #     """Path to save configuration."""
    #     return os.path.join(self.run_dir, "config.json")