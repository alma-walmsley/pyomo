from experiment_class_example import *
from pyomo.contrib.doe import *

import numpy as np

doe_obj = [0, 0, 0,]

for ind, fd in enumerate(['central', 'backward', 'forward']):
    print(fd)
    experiment = FullReactorExperiment(data_ex, 32, 3)
    doe_obj[ind] = DesignOfExperiments_(experiment, fd_formula=fd)
    doe_obj[ind].jac_initial = None
    doe_obj[ind].prior_FIM = np.eye(4)
    doe_obj[ind].fim_initial = None
    doe_obj[ind].L_initial = None
    doe_obj[ind].L_LB = 1e-7
    doe_obj[ind].Cholesky_option = True
    doe_obj[ind].objective_option = ObjectiveLib.det
    doe_obj[ind].scale_nominal_param_value = True
    doe_obj[ind].scale_constant_value = 1
    doe_obj[ind].step = 0.001
    doe_obj[ind].create_doe_model()