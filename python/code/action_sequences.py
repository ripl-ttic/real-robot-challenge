#!/usr/bin/env python3
from code.utils import frameskip_to, action_type_to, repeat, get_yaw_diff
from code.const import TRANSLU_CYAN, CUBOID_SIZE, INIT_JOINT_CONF
from code.align_rotation import project_cube_xy_plane
from code.env.cube_env import ActionType
from scipy.spatial.transform import Rotation as R
import numpy as np
import time


def complete_keypoints(start, goal, unit_length=0.008):
    assert start.shape == goal.shape
    assert len(start.shape) in [1, 2]
    diff = goal - start
    if len(start.shape) == 2:
        length = max(np.linalg.norm(diff, axis=1))
    else:
        length = np.linalg.norm(diff)

    num_keypoints = int(length / unit_length)
    keypoints = [start + diff * i / num_keypoints for i in range(num_keypoints)]
    return keypoints


class ScriptedActions(object):
    def __init__(self, env, robot_tip_positions, grasp, vis_markers=None):
        self.env = env
        self.grasp = grasp
        self.robot_tip_positions = robot_tip_positions
        self.tip_positions_list = []
        self.executed = False
        self.vis_markers = vis_markers
        self._markers = set()

    def _update_markers(self, target_tip_positions, marker_name,
                        color=TRANSLU_CYAN):
        if self.vis_markers is not None:
            if marker_name in self._markers:
                self.vis_markers.remove()
            self.vis_markers.add(target_tip_positions, color=color)
            self._markers.add(marker_name)

    def add_move(self, tip_pos, unit_length, min_height=0.01):
        current_tip_pos = self.get_last_tippos()

        if np.any(tip_pos[:, 2] < min_height):
            tip_pos[:, 2] = np.maximum(tip_pos[:, 2], min_height)

        # do not move tip_pos if the corresponding tip is invalid
        mask = np.eye(3)[self.grasp.valid_tips, :].sum(0).reshape(3, -1)
        tip_pos = tip_pos * mask + (1 - mask) * self.robot_tip_positions
        self.tip_positions_list += complete_keypoints(current_tip_pos, tip_pos,
                                                      unit_length=unit_length)

    def add_grasp(self, coef=0.9):
        target_tip_positions = self.grasp.T_cube_to_base(
            self.grasp.cube_tip_pos * coef
        )
        self._update_markers(target_tip_positions, 'grasp')
        self.add_move(target_tip_positions, 0.004)

    def add_release(self, coef=2.0, min_height=0.01):
        target_tip_positions = self.grasp.T_cube_to_base(
            self.grasp.cube_tip_pos * coef
        )
        if np.any(target_tip_positions[:, 2] < min_height):
            target_tip_positions[:, 2] = np.maximum(target_tip_positions[:, 2], min_height)

        self.add_move(target_tip_positions, 0.004)

    def add_release2(self, coef=2.0, min_height=0.01):
        tip_pos = self.get_last_tippos()
        center = np.mean(tip_pos, axis=0)
        target_tip_positions = center + (tip_pos - center) * coef
        if np.any(target_tip_positions[:, 2] < min_height):
            target_tip_positions[:, 2] = np.maximum(target_tip_positions[:, 2], min_height)
        self.add_move(target_tip_positions, 0.004)

    def add_move_to_center(self, coef=0.6):
        self.grasp.update(np.zeros(3), self.grasp.quat)
        self.add_move(
            self.grasp.T_cube_to_base(self.grasp.cube_tip_pos * coef),
            0.004
        )

    def add_raise_tips(self, height=CUBOID_SIZE[0] * 2):
        target_tip_pos = self.get_last_tippos()
        target_tip_pos[:, 2] = height
        self.add_move(target_tip_pos, 0.004)

    def add_heuristic_pregrasp(self, pregrasp_tip_pos):
        if self.get_last_tippos()[:, 2].min() < CUBOID_SIZE[0] / 2:
            print('Warning: adding heuristic pregrasp even though robot_tip postiion is low')
        above_target_tip_positions = np.copy(pregrasp_tip_pos)
        above_target_tip_positions[:, 2] = CUBOID_SIZE[0] * 2
        self.add_move(above_target_tip_positions, 0.004)
        self.add_move(pregrasp_tip_pos, 0.004)

    def add_pitch_rotation(self, height, rotate_axis, rotate_angle, coef=0.6):
        # lift cube up
        self.grasp.update(self.grasp.pos + np.array([0, 0, height]),
                          self.grasp.quat)
        target_tip_positions = self.grasp.T_cube_to_base(
            self.grasp.cube_tip_pos * coef
        )
        self._update_markers(target_tip_positions, 'liftup')
        self.add_move(target_tip_positions, 0.004)

        # rotate cube
        rotate_step = np.sign(rotate_angle) * np.pi / 30
        rot = R.from_euler(rotate_axis, rotate_step)
        print(f'add_pitch_rotation: rotate_axis {rotate_axis}\trotate_angle {rotate_angle}')
        for _ in range(int(rotate_angle / rotate_step)):
            orientation = (R.from_quat(self.grasp.quat) * rot).as_quat()
            self.grasp.update(self.grasp.pos, orientation)
            target_tip_positions = self.grasp.T_cube_to_base(
                self.grasp.cube_tip_pos * coef
            )
            self.tip_positions_list.append(target_tip_positions)

        # place_cube
        self.grasp.update(self.grasp.pos - np.array([0, 0, height]),
                          self.grasp.quat)
        self.add_move(
            self.grasp.T_cube_to_base(self.grasp.cube_tip_pos * coef),
            0.004
        )

    def add_yaw_rotation(self, goal_quat, step_angle=np.pi/3, coef=0.9):
        angle = get_yaw_diff(self.grasp.quat, goal_quat)
        angle_clipped = np.clip(angle, -step_angle, step_angle)
        ori = (
            R.from_euler('Z', angle_clipped)
            * R.from_quat(self.grasp.quat)
        ).as_quat()
        self.grasp.update(self.grasp.pos, ori)
        target_tip_positions = self.grasp.T_cube_to_base(
            self.grasp.cube_tip_pos * coef
        )
        self._update_markers(target_tip_positions, 'yaw')
        self.add_move(target_tip_positions, 0.002)
        return angle_clipped

    def get_last_tippos(self):
        if len(self.tip_positions_list) == 0:
            return np.copy(self.robot_tip_positions)
        else:
            return np.copy(self.tip_positions_list[-1])

    def _tip_positions_to_actions(self):
        ik = self.env.pinocchio_utils.inverse_kinematics

        actions = []
        skip_count = 0
        for tip_positions in self.tip_positions_list:
            q = INIT_JOINT_CONF.copy()
            for i in range(3):
                q = ik(i, tip_positions[i], q)
                if q is None:
                    print('Warning: IK solution not found (tip_positions_to_actions)')
                    break
            if q is not None:
                for _ in range(skip_count + 1):
                    actions.append(q)
                skip_count = 1
            else:
                skip_count += 1
        return actions

    def execute_motion(self, frameskip, action_repeat, action_repeat_end):
        if self.executed:
            raise ValueError("You can only execute the action sequence once!")
        # run the action sequence
        action_seq = self._tip_positions_to_actions()
        action_seq = repeat(action_seq, action_repeat)
        action_seq += repeat([action_seq[-1]], action_repeat_end)
        step_start = self.env.unwrapped.step_count
        with frameskip_to(frameskip, self.env):
            with action_type_to(ActionType.POSITION, self.env):
                for action in action_seq:
                    action = np.asarray(action)
                    obs, reward, done, info = self.env.step(action)
                    if done:
                        break
        step_end = self.env.unwrapped.step_count
        print("step: {}".format(step_end - step_start))
        self.executed = True
        return obs, done


if __name__ == '__main__':
    import pybullet as p
    from code.make_env import make_training_env
    from trifinger_simulation.tasks import move_cube
    from code.grasping import get_heuristic_grasp, get_pitching_grasp
    from code.grasping import get_yawing_grasp, execute_grasp_approach
    from code.align_rotation import pitch_rotation_axis_and_angle

    env = make_training_env(move_cube.sample_goal(-1).to_dict(), 3,
                            reward_fn='competition_reward',
                            termination_fn='position_close_to_goal',
                            initializer='training_init',
                            action_space='torque',
                            sim=True,
                            visualization=True,
                            rank=1)

    obs = env.reset()
    p.resetDebugVisualizerCamera(cameraDistance=0.6, cameraYaw=0,
                                 cameraPitch=-40,
                                 cameraTargetPosition=[0, 0, 0])

    # move to center
    grasp = get_heuristic_grasp(env, obs['object_position'],
                                obs['object_orientation'])
    obs, done = execute_grasp_approach(env, obs, grasp)

    action_sequence = ScriptedActions(env, obs['robot_tip_positions'], grasp)
    action_sequence.add_grasp(coef=0.6)
    action_sequence.add_move_to_center(coef=0.6)
    action_sequence.add_release()
    obs, done = action_sequence.execute_motion(3, 5, 10)

    # pitch rotation
    grasp = get_pitching_grasp(env, obs['object_position'],
                               obs['object_orientation'],
                               obs['goal_object_orientation'])
    obs, done = execute_grasp_approach(env, obs, grasp)
    action_sequence = ScriptedActions(env, obs['robot_tip_positions'], grasp)
    action_sequence.add_grasp(coef=0.6)
    rotate_axis, rotate_angle = pitch_rotation_axis_and_angle(grasp.cube_tip_pos)
    action_sequence.add_pitch_rotation(0.045, rotate_axis, rotate_angle,
                                       coef=0.6)
    action_sequence.add_release()
    obs, done = action_sequence.execute_motion(3, 5, 10)

    # yaw rotation
    grasp = get_yawing_grasp(env, obs['object_position'],
                             obs['object_orientation'],
                             obs['goal_object_orientation'])
    obs, done = execute_grasp_approach(env, obs, grasp)

    action_sequence = ScriptedActions(env, obs['robot_tip_positions'], grasp)
    action_sequence.add_grasp(coef=0.6)
    action_sequence.add_yaw_rotation(obs['goal_object_orientation'])
    action_sequence.add_release()

    action_sequence.execute_motion(3, 5, 10)
