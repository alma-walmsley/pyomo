
# -*- coding: utf-8 -*-

#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright (c) 2008-2022
#  National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________

import logging
from pyomo.contrib.gdpopt.util import (time_code, lower_logger_level_to, copy_var_list_values)
from pyomo.contrib.mindtpy.util import set_up_logger, setup_results_object, add_var_bound, calc_jacobians
from pyomo.core import TransformationFactory, maximize, Objective, ConstraintList
from pyomo.opt import SolverFactory
from pyomo.contrib.mindtpy.config_options import _get_MindtPy_OA_config
from pyomo.contrib.mindtpy.algorithm_base_class import _MindtPyAlgorithm
from pyomo.contrib.mindtpy.util import get_integer_solution, copy_var_list_values_from_solution_pool
from pyomo.solvers.plugins.solvers.gurobi_direct import gurobipy
from operator import itemgetter
from pyomo.opt import TerminationCondition as tc
from pyomo.contrib.mindtpy.cut_generation import add_oa_cuts


@SolverFactory.register(
    'mindtpy.oa',
    doc='MindtPy: Mixed-Integer Nonlinear Decomposition Toolbox in Pyomo')
class MindtPy_OA_Solver(_MindtPyAlgorithm):
    """
    Decomposition solver for Mixed-Integer Nonlinear Programming (MINLP) problems.

    The MindtPy (Mixed-Integer Nonlinear Decomposition Toolbox in Pyomo) solver 
    applies a variety of decomposition-based approaches to solve Mixed-Integer 
    Nonlinear Programming (MINLP) problems. 
    These approaches include:

    - Outer approximation (OA)
    - Global outer approximation (GOA)
    - Regularized outer approximation (ROA)
    - LP/NLP based branch-and-bound (LP/NLP)
    - Global LP/NLP based branch-and-bound (GLP/NLP)
    - Regularized LP/NLP based branch-and-bound (RLP/NLP)
    - Feasibility pump (FP)

    This solver implementation has been developed by David Bernal <https://github.com/bernalde>
    and Zedong Peng <https://github.com/ZedongPeng> as part of research efforts at the Grossmann
    Research Group (http://egon.cheme.cmu.edu/) at the Department of Chemical Engineering at 
    Carnegie Mellon University.
    """
    CONFIG = _get_MindtPy_OA_config()


    def solve(self, model, **kwds):
        """Solve the model.

        Parameters
        ----------
        model : Pyomo model
            The MINLP model to be solved.

        Returns
        -------
        results : SolverResults
            Results from solving the MINLP problem by MindtPy.
        """
        config = self.config = self.CONFIG(kwds.pop('options', {}), preserve_implicit=True)
        config.set_value(kwds)
        set_up_logger(config)
        new_logging_level = logging.INFO if config.tee else None
        with lower_logger_level_to(config.logger, new_logging_level):
            self.check_config()

        self.set_up_solve_data(model, config)

        if config.integer_to_binary:
            TransformationFactory('contrib.integer_to_binary'). \
                apply_to(self.working_model)

        self.create_utility_block(self.working_model, 'MindtPy_utils')
        with time_code(self.timing, 'total', is_main_timer=True), \
                lower_logger_level_to(config.logger, new_logging_level):
            self._log_solver_intro_message()

            # Validate the model to ensure that MindtPy is able to solve it.
            if not self.model_is_valid():
                return

            MindtPy = self.working_model.MindtPy_utils
            setup_results_object(self.results, self.original_model, config)
            # In the process_objective function, as long as the objective function is nonlinear, it will be reformulated and the variable/constraint/objective lists will be updated.
            # For OA/GOA/LP-NLP algorithm, if the objective funtion is linear, it will not be reformulated as epigraph constraint.
            # If the objective function is linear, it will be reformulated as epigraph constraint only if the Feasibility Pump or ROA/RLP-NLP algorithm is activated. (move_objective = True)
            # In some cases, the variable/constraint/objective lists will not be updated even if the objective is epigraph-reformulated.
            # In Feasibility Pump, since the distance calculation only includes discrete variables and the epigraph slack variables are continuous variables, the Feasibility Pump algorithm will not affected even if the variable list are updated.
            # In ROA and RLP/NLP, since the distance calculation does not include these epigraph slack variables, they should not be added to the variable list. (update_var_con_list = False)
            # In the process_objective function, once the objective function has been reformulated as epigraph constraint, the variable/constraint/objective lists will not be updated only if the MINLP has a linear objective function and regularization is activated at the same time.
            # This is because the epigraph constraint is very "flat" for branching rules. The original objective function will be used for the main problem and epigraph reformulation will be used for the projection problem.
            # TODO: The logic here is too complicated, can we simplify it?
            self.process_objective(config,
                                   move_objective=config.move_objective,
                                   use_mcpp=config.use_mcpp,
                                   update_var_con_list=config.add_regularization is None,
                                   partition_nonlinear_terms=config.partition_obj_nonlinear_terms,
                                   obj_handleable_polynomial_degree=self.mip_objective_polynomial_degree,
                                   constr_handleable_polynomial_degree=self.mip_constraint_polynomial_degree)
            # The epigraph constraint is very "flat" for branching rules.
            # If ROA/RLP-NLP is activated and the original objective function is linear, we will use the original objective for the main mip.
            if MindtPy.objective_list[0].expr.polynomial_degree() in self.mip_objective_polynomial_degree and config.add_regularization is not None:
                MindtPy.objective_list[0].activate()
                MindtPy.objective_constr.deactivate()
                MindtPy.objective.deactivate()

            # Save model initial values.
            self.initial_var_values = list(
                v.value for v in MindtPy.variable_list)

            # TODO: if the MindtPy solver is defined once and called several times to solve models. The following two lines are necessary. It seems that the solver class will not be init every time call.
            # For example, if we remove the following two lines. test_RLPNLP_L1 will fail.
            self.best_solution_found = None
            self.best_solution_found_time = None
            self.initialize_mip_problem()

            # Initialization
            with time_code(self.timing, 'initialization'):
                self.MindtPy_initialization(config)

            # Algorithm main loop
            with time_code(self.timing, 'main loop'):
                self.MindtPy_iteration_loop(config)

            # Load solution
            if self.best_solution_found is not None:
                self.load_solution()

            # Get integral info
            self.get_integral_info()

            config.logger.info(' {:<25}:   {:>7.4f} '.format(
                'Primal-dual gap integral', self.primal_dual_gap_integral))

        # Update result
        self.update_result()
        if config.single_tree:
            self.results.solver.num_nodes = self.nlp_iter - \
                (1 if config.init_strategy == 'rNLP' else 0)

        return self.results


    # iterate.py
    def MindtPy_iteration_loop(self, config):
        """Main loop for MindtPy Algorithms.

        This is the outermost function for the Outer Approximation algorithm in this package; this function controls the progression of
        solving the model.

        Parameters
        ----------
        config : ConfigBlock
            The specific configurations for MindtPy.

        Raises
        ------
        ValueError
            The strategy value is not correct or not included.
        """
        last_iter_cuts = False
        while self.mip_iter < config.iteration_limit:

            self.mip_subiter = 0
            # solve MILP main problem
            main_mip, main_mip_results = self.solve_main(config)
            if main_mip_results is not None:
                if not config.single_tree:
                    if main_mip_results.solver.termination_condition is tc.optimal:
                        self.handle_main_optimal(main_mip, config)
                    elif main_mip_results.solver.termination_condition is tc.infeasible:
                        self.handle_main_infeasible(main_mip, config)
                        last_iter_cuts = True
                        break
                    else:
                        self.handle_main_other_conditions(
                            main_mip, main_mip_results, config)
                    # Call the MILP post-solve callback
                    with time_code(self.timing, 'Call after main solve'):
                        config.call_after_main_solve(main_mip)
            else:
                config.logger.info('Algorithm should terminate here.')
                break

            # Regularization is activated after the first feasible solution is found.
            if config.add_regularization is not None and self.best_solution_found is not None and not config.single_tree:
                # The main problem might be unbounded, regularization is activated only when a valid bound is provided.
                if self.dual_bound != self.dual_bound_progress[0]:
                    main_mip, main_mip_results = self.solve_main(config, regularization_problem=True)
                    self.handle_regularization_main_tc(main_mip, main_mip_results, config)

            # TODO: add descriptions for the following code
            if config.add_regularization is not None and config.single_tree:
                self.curr_int_sol = get_integer_solution(
                    self.mip, string_zero=True)
                copy_var_list_values(
                    main_mip.MindtPy_utils.variable_list,
                    self.working_model.MindtPy_utils.variable_list,
                    config)
                if self.curr_int_sol not in set(self.integer_list):
                    fixed_nlp, fixed_nlp_result = self.solve_subproblem(config)
                    self.handle_nlp_subproblem_tc(fixed_nlp, fixed_nlp_result, config)
            if self.algorithm_should_terminate(config, check_cycling=True):
                last_iter_cuts = False
                break
            if not config.single_tree:  # if we don't use lazy callback, i.e. LP_NLP
                # Solve NLP subproblem
                # The constraint linearization happens in the handlers
                if not config.solution_pool:
                    fixed_nlp, fixed_nlp_result = self.solve_subproblem(config)
                    self.handle_nlp_subproblem_tc(fixed_nlp, fixed_nlp_result, config)

                    # Call the NLP post-solve callback
                    with time_code(self.timing, 'Call after subproblem solve'):
                        config.call_after_subproblem_solve(fixed_nlp)

                    if self.algorithm_should_terminate(config, check_cycling=False):
                        last_iter_cuts = True
                        break
                else:
                    if config.mip_solver == 'cplex_persistent':
                        solution_pool_names = main_mip_results._solver_model.solution.pool.get_names()
                    elif config.mip_solver == 'gurobi_persistent':
                        solution_pool_names = list(
                            range(main_mip_results._solver_model.SolCount))
                    # list to store the name and objective value of the solutions in the solution pool
                    solution_name_obj = []
                    for name in solution_pool_names:
                        if config.mip_solver == 'cplex_persistent':
                            obj = main_mip_results._solver_model.solution.pool.get_objective_value(
                                name)
                        elif config.mip_solver == 'gurobi_persistent':
                            main_mip_results._solver_model.setParam(
                                gurobipy.GRB.Param.SolutionNumber, name)
                            obj = main_mip_results._solver_model.PoolObjVal
                        solution_name_obj.append([name, obj])
                    solution_name_obj.sort(
                        key=itemgetter(1), reverse=self.objective_sense == maximize)
                    counter = 0
                    for name, _ in solution_name_obj:
                        # the optimal solution of the main problem has been added to integer_list above
                        # so we should skip checking cycling for the first solution in the solution pool
                        if counter >= 1:
                            copy_var_list_values_from_solution_pool(self.mip.MindtPy_utils.variable_list,
                                                                    self.working_model.MindtPy_utils.variable_list,
                                                                    config, solver_model=main_mip_results._solver_model,
                                                                    var_map=main_mip_results._pyomo_var_to_solver_var_map,
                                                                    solution_name=name)
                            self.curr_int_sol = get_integer_solution(
                                self.working_model)
                            if self.curr_int_sol in set(self.integer_list):
                                config.logger.info(
                                    'The same combination has been explored and will be skipped here.')
                                continue
                            else:
                                self.integer_list.append(
                                    self.curr_int_sol)
                        counter += 1
                        fixed_nlp, fixed_nlp_result = self.solve_subproblem(config)
                        self.handle_nlp_subproblem_tc(fixed_nlp, fixed_nlp_result, config)

                        # Call the NLP post-solve callback
                        with time_code(self.timing, 'Call after subproblem solve'):
                            config.call_after_subproblem_solve(fixed_nlp)

                        if self.algorithm_should_terminate(config, check_cycling=False):
                            last_iter_cuts = True
                            break

                        if counter >= config.num_solution_iteration:
                            break

            # if config.strategy == 'PSC':
            #     # If the hybrid algorithm is not making progress, switch to OA.
            #     progress_required = 1E-6
            #     if solve_data.objective_sense == minimize:
            #         log = solve_data.LB_progress
            #         sign_adjust = 1
            #     else:
            #         log = solve_data.UB_progress
            #         sign_adjust = -1
            #     # Maximum number of iterations in which the lower (optimistic)
            #     # bound does not improve before switching to OA
            #     max_nonimprove_iter = 5
            #     making_progress = True
            #     # TODO-romeo Unnecessary for OA and ROA, right?
            #     for i in range(1, max_nonimprove_iter + 1):
            #         try:
            #             if (sign_adjust * log[-i]
            #                     <= (log[-i - 1] + progress_required)
            #                     * sign_adjust):
            #                 making_progress = False
            #             else:
            #                 making_progress = True
            #                 break
            #         except IndexError:
            #             # Not enough history yet, keep going.
            #             making_progress = True
            #             break
            #     if not making_progress and (
            #             config.strategy == 'hPSC' or
            #             config.strategy == 'PSC'):
            #         config.logger.info(
            #             'Not making enough progress for {} iterations. '
            #             'Switching to OA.'.format(max_nonimprove_iter))
            #         config.strategy = 'OA'

        # if add_no_good_cuts is True, the bound obtained in the last iteration is no reliable.
        # we correct it after the iteration.
        if (config.add_no_good_cuts or config.use_tabu_list) and not self.should_terminate and config.add_regularization is None:
            self.fix_dual_bound(config, last_iter_cuts)
        config.logger.info(
            ' ===============================================================================================')

    def check_config(self):
        config = self.config
        if config.add_regularization is not None:
            if config.add_regularization in {'grad_lag', 'hess_lag', 'hess_only_lag', 'sqp_lag'}:
                config.calculate_dual_at_solution = True
            if config.regularization_mip_threads == 0 and config.threads > 0:
                config.regularization_mip_threads = config.threads
                config.logger.info(
                    'Set regularization_mip_threads equal to threads')
            if config.single_tree:
                config.add_cuts_at_incumbent = True
                # if no method is activated by users, we will use use_bb_tree_incumbent by default
                if not (config.reduce_level_coef or config.use_bb_tree_incumbent):
                    config.use_bb_tree_incumbent = True
            if config.mip_regularization_solver is None:
                config.mip_regularization_solver = config.mip_solver
        if config.single_tree:
            config.logger.info('Single-tree implementation is activated.')
            config.iteration_limit = 1
            config.add_slack = False
            if config.mip_solver not in {'cplex_persistent', 'gurobi_persistent'}:
                raise ValueError("Only cplex_persistent and gurobi_persistent are supported for LP/NLP based Branch and Bound method."
                                "Please refer to https://pyomo.readthedocs.io/en/stable/contributed_packages/mindtpy.html#lp-nlp-based-branch-and-bound.")
            if config.threads > 1:
                config.threads = 1
                config.logger.info(
                    'The threads parameter is corrected to 1 since lazy constraint callback conflicts with multi-threads mode.')
        if config.heuristic_nonconvex:
            config.equality_relaxation = True
            config.add_slack = True
        if config.equality_relaxation:
            config.calculate_dual_at_solution = True
        if config.init_strategy == 'FP' or config.add_regularization is not None:
            config.move_objective = True
        if config.add_regularization is not None:
            if config.add_regularization in {'level_L1', 'level_L_infinity', 'grad_lag'}:
                self.regularization_mip_type = 'MILP'
            elif config.add_regularization in {'level_L2', 'hess_lag', 'hess_only_lag', 'sqp_lag'}:
                self.regularization_mip_type = 'MIQP'
        _MindtPyAlgorithm.check_config(self)


    def initialize_mip_problem(self):
        ''' Deactivate the nonlinear constraints to create the MIP problem.
        '''
        # if single tree is activated, we need to add bounds for unbounded variables in nonlinear constraints to avoid unbounded main problem.
        config = self.config
        if config.single_tree:
            add_var_bound(self.working_model, config)

        m = self.mip = self.working_model.clone()
        next(self.mip.component_data_objects(
            Objective, active=True)).deactivate()

        MindtPy = m.MindtPy_utils
        if config.calculate_dual_at_solution:
            m.dual.deactivate()

        self.jacobians = calc_jacobians(self.mip, config)  # preload jacobians
        MindtPy.cuts.oa_cuts = ConstraintList(doc='Outer approximation cuts')

        if config.init_strategy == 'FP':
            MindtPy.cuts.fp_orthogonality_cuts = ConstraintList(
                doc='Orthogonality cuts in feasibility pump')
            if config.fp_projcuts:
                self.working_model.MindtPy_utils.cuts.fp_orthogonality_cuts = ConstraintList(
                    doc='Orthogonality cuts in feasibility pump')


    def add_cuts(self,
                 dual_values,
                 linearize_active=True,
                 linearize_violated=True,
                 cb_opt=None):
        add_oa_cuts(self.mip, 
                    dual_values,
                    self.jacobians,
                    self.objective_sense,
                    self.mip_constraint_polynomial_degree,
                    self.mip_iter,
                    self.config,
                    self.timing,
                    cb_opt,
                    linearize_active,
                    linearize_violated)


    def deactivate_no_good_cuts_when_fixing_bound(self, no_good_cuts):
        # Only deactive the last OA cuts may not be correct.
        # Since integer solution may also be cut off by OA cuts due to calculation approximation.
        if self.config.add_no_good_cuts:
            no_good_cuts[len(no_good_cuts)].deactivate()
        if self.config.use_tabu_list:
            self.integer_list = self.integer_list[:-1]
