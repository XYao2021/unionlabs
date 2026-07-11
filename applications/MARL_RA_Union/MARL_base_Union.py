#%% Import packages -----------------------------------------------------------
import os
import random
import numpy as np
import torch

#%% defines -------------------------------------------------------------------

def set_seed_everywhere(seed):
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)

def make_dir(dir_path):
    try:
        os.makedirs(dir_path)
    except OSError:
        pass
    return dir_path

def dBToLinear(val):
    return 10**(val/10)

def LinearTodB(val):
    return 10*np.log10(val)

def get_Gaussian(mean=0.0, var=1.0, size=(1,), sig_type='complex'):
    if sig_type == 'complex':
        return np.sqrt(0.5)*np.random.normal(loc=mean, scale=np.sqrt(var), size=size) + 1j*np.sqrt(0.5)*np.random.normal(loc=mean, scale=np.sqrt(var), size=size)
    elif sig_type == 'real':
        return np.random.normal(loc=mean, scale=np.sqrt(var), size=size)
    else:
        print('incorrect sig_type for get_Gaussian()')

def get_moving_avg(arr, window_size):
    cumsum = np.cumsum(arr)
    moving_avg = (cumsum[window_size:] - cumsum[:-window_size]) / window_size
    return moving_avg

def store_sum(list_sum, list_score):
    if len(list_sum) == 0:
        list_sum.append(list_score)
    else:
        list_sum.append(list_sum[-1] + list_score)

def permute_symmetric_off_diagonal(matrix):
    assert matrix.shape[0] == matrix.shape[1], "Matrix must be square"
    n = matrix.shape[0]
    i_upper, j_upper = np.triu_indices(n, k=1) # Get upper triangle indices (excluding diagonal)
    values = matrix[i_upper, j_upper] # Extract upper-triangle values
    np.random.shuffle(values) # Permute them
    new_matrix = np.diag(np.diag(matrix)) # Create a new matrix with the same diagonal
    new_matrix[i_upper, j_upper] = values # Fill upper triangle
    new_matrix[j_upper, i_upper] = values # Mirror to lower triangle to ensure symmetry
    return new_matrix

def organize_arr(total_arr, num_E, num_R, condition):
    # condition: 0 for greater is better, 1 for less is better, 2 for as is
    if condition != 2:
        tmp = np.array(total_arr)
        new_arr = np.zeros(np.shape(tmp))
        for a in range(num_R):
            for b in range(num_E):
                idx = np.argsort(tmp[a, :, b])
                if condition == 0:
                    new_arr[a, :, b] = tmp[a, np.flip(idx), b]
                elif condition == 1:
                    new_arr[a, :, b] = tmp[a, idx, b]
        return np.mean(np.array(new_arr), axis=0), np.array(new_arr)
    else:
        return np.mean(np.array(total_arr), axis=0), np.array(total_arr)