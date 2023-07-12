# Bed leveling sensor BDsensor(Bed Distance sensor)
# https://github.com/markniu/Bed_Distance_sensor
# Copyright (C) 2023 Mark yue <niujl123@sina.com>
# This file may be distributed under the terms of the GNU GPLv3 license.
import sched, time
from threading import Timer

import chelper
import math

import logging
import pins
from . import manual_probe

HINT_TIMEOUT = """
If the probe did not move far enough to trigger, then
consider reducing the Z axis minimum position so the probe
can travel further (the Z minimum position can be negative).
"""

class PrinterProbe:
    def __init__(self, config, mcu_probe):
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.mcu_probe = mcu_probe
        self.speed = config.getfloat('speed', 5.0, above=0.)
        self.lift_speed = config.getfloat('lift_speed', self.speed, above=0.)
        self.x_offset = config.getfloat('x_offset', 0.)
        self.y_offset = config.getfloat('y_offset', 0.)
        self.z_offset = config.getfloat('z_offset')
        self.probe_calibrate_z = 0.
        self.multi_probe_pending = False
        self.last_state = False
        self.last_z_result = 0.
        self.gcode_move = self.printer.load_object(config, "gcode_move")
        # Infer Z position to move to during a probe
        if config.has_section('stepper_z'):
            zconfig = config.getsection('stepper_z')
            self.z_position = zconfig.getfloat('position_min', 0.,
                                               note_valid=False)
        else:
            pconfig = config.getsection('printer')
            self.z_position = pconfig.getfloat('minimum_z_position', 0.,
                                               note_valid=False)
        # Multi-sample support (for improved accuracy)
        self.sample_count = config.getint('samples', 1, minval=1)
        self.sample_retract_dist = config.getfloat('sample_retract_dist', 2.,
                                                   above=0.)
        atypes = {'median': 'median', 'average': 'average'}
        self.samples_result = config.getchoice('samples_result', atypes,
                                               'average')
        self.samples_tolerance = config.getfloat('samples_tolerance', 0.100,
                                                 minval=0.)
        self.samples_retries = config.getint('samples_tolerance_retries', 0,
                                             minval=0)
        # Register z_virtual_endstop pin
        self.printer.lookup_object('pins').register_chip('probe', self)
        # Register homing event handlers
        self.printer.register_event_handler("homing:homing_move_begin",
                                            self._handle_homing_move_begin)
        self.printer.register_event_handler("homing:homing_move_end",
                                            self._handle_homing_move_end)
        self.printer.register_event_handler("homing:home_rails_begin",
                                            self._handle_home_rails_begin)
        self.printer.register_event_handler("homing:home_rails_end",
                                            self._handle_home_rails_end)
        self.printer.register_event_handler("gcode:command_error",
                                            self._handle_command_error)
        # Register PROBE/QUERY_PROBE commands
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command('PROBE', self.cmd_PROBE,
                                    desc=self.cmd_PROBE_help)
        self.gcode.register_command('QUERY_PROBE', self.cmd_QUERY_PROBE,
                                    desc=self.cmd_QUERY_PROBE_help)
        self.gcode.register_command('PROBE_CALIBRATE', self.cmd_PROBE_CALIBRATE,
                                    desc=self.cmd_PROBE_CALIBRATE_help)
        self.gcode.register_command('PROBE_ACCURACY', self.cmd_PROBE_ACCURACY,
                                    desc=self.cmd_PROBE_ACCURACY_help)
        self.gcode.register_command('Z_OFFSET_APPLY_PROBE',
                                    self.cmd_Z_OFFSET_APPLY_PROBE,
                                    desc=self.cmd_Z_OFFSET_APPLY_PROBE_help)
    def _handle_homing_move_begin(self, hmove):
        if self.mcu_probe in hmove.get_mcu_endstops():
            self.mcu_probe.probe_prepare(hmove)
    def _handle_homing_move_end(self, hmove):
        if self.mcu_probe in hmove.get_mcu_endstops():
            self.mcu_probe.probe_finish(hmove)
    def _handle_home_rails_begin(self, homing_state, rails):
        endstops = [es for rail in rails for es, name in rail.get_endstops()]
        if self.mcu_probe in endstops:
            self.multi_probe_begin()
    def _handle_home_rails_end(self, homing_state, rails):
        endstops = [es for rail in rails for es, name in rail.get_endstops()]
        if self.mcu_probe in endstops:
            self.multi_probe_end()
    def _handle_command_error(self):
        try:
            self.multi_probe_end()
        except:
            logging.exception("Multi-probe end")
    def multi_probe_begin(self):
        self.mcu_probe.multi_probe_begin()
        self.multi_probe_pending = True
    def multi_probe_end(self):
        if self.multi_probe_pending:
            self.multi_probe_pending = False
            self.mcu_probe.multi_probe_end()
    def setup_pin(self, pin_type, pin_params):
        if pin_type != 'endstop' or pin_params['pin'] != 'z_virtual_endstop':
            raise pins.error("Probe virtual endstop only useful as endstop pin")
        if pin_params['invert'] or pin_params['pullup']:
            raise pins.error("Can not pullup/invert probe virtual endstop")
        return self.mcu_probe
    def get_lift_speed(self, gcmd=None):
        if gcmd is not None:
            return gcmd.get_float("LIFT_SPEED", self.lift_speed, above=0.)
        return self.lift_speed
    def get_offsets(self):
        return self.x_offset, self.y_offset, self.z_offset
    def _probe(self, speed):
        toolhead = self.printer.lookup_object('toolhead')
        curtime = self.printer.get_reactor().monotonic()
        if 'z' not in toolhead.get_status(curtime)['homed_axes']:
            raise self.printer.command_error("Must home before probe")
        phoming = self.printer.lookup_object('homing')
        pos = toolhead.get_position()
        pos[2] = self.z_position
        try:
            epos = phoming.probing_move(self.mcu_probe, pos, speed)
        except self.printer.command_error as e:
            reason = str(e)
            if "Timeout during endstop homing" in reason:
                reason += HINT_TIMEOUT
            raise self.printer.command_error(reason)
        self.gcode.respond_info("probe at %.3f,%.3f is z=%.6f"
                                % (epos[0], epos[1], epos[2]))
        return epos[:3]
    def _move(self, coord, speed):
        self.printer.lookup_object('toolhead').manual_move(coord, speed)
    def _calc_mean(self, positions):
        count = float(len(positions))
        return [sum([pos[i] for pos in positions]) / count
                for i in range(3)]
    def _calc_median(self, positions):
        z_sorted = sorted(positions, key=(lambda p: p[2]))
        middle = len(positions) // 2
        if (len(positions) & 1) == 1:
            # odd number of samples
            return z_sorted[middle]
        # even number of samples
        return self._calc_mean(z_sorted[middle-1:middle+1])
    def run_probe(self, gcmd):
        speed = gcmd.get_float("PROBE_SPEED", self.speed, above=0.)
        lift_speed = self.get_lift_speed(gcmd)
        sample_count = gcmd.get_int("SAMPLES", self.sample_count, minval=1)
        sample_retract_dist = gcmd.get_float("SAMPLE_RETRACT_DIST",
                                             self.sample_retract_dist, above=0.)
        samples_tolerance = gcmd.get_float("SAMPLES_TOLERANCE",
                                           self.samples_tolerance, minval=0.)
        samples_retries = gcmd.get_int("SAMPLES_TOLERANCE_RETRIES",
                                       self.samples_retries, minval=0)
        samples_result = gcmd.get("SAMPLES_RESULT", self.samples_result)
        must_notify_multi_probe = not self.multi_probe_pending
        if must_notify_multi_probe:
            self.multi_probe_begin()
        probexy = self.printer.lookup_object('toolhead').get_position()[:2]
        retries = 0
        positions = []
        toolhead = self.printer.lookup_object('toolhead')
        #gcmd.respond_info("speed:%.3f"%speed)
        while len(positions) < sample_count:         
            # Probe position
            try:
                if ((self.mcu_probe.bd_sensor is not None) and 
                        ((gcmd.get_command() == "BED_MESH_CALIBRATE") or
                        (gcmd.get_command() == "QUAD_GANTRY_LEVEL"))):
                    #pos = self._probe(speed)
                    toolhead.wait_moves()
                    time.sleep(0.004)
                    pos = toolhead.get_position()
                    intd=self.mcu_probe.BD_Sensor_Read(0)
                    pos[2]=pos[2]-intd
                    self.gcode.respond_info("probe at %.3f,%.3f is z=%.6f"
                                            % (pos[0], pos[1], pos[2]))
                    #return pos[:3]
                    positions.append(pos[:3])
                    # Check samples tolerance
                    z_positions = [p[2] for p in positions]
                    if max(z_positions) - min(z_positions) > samples_tolerance:
                        if retries >= samples_retries:
                            raise gcmd.error("Probe samples exceed samples_tolerance")
                        gcmd.respond_info("Probe samples exceed tolerance. Retrying...")
                        retries += 1
                        positions = []
                    continue
            except Exception as e:
                gcmd.respond_info("%s"%str(e))
                pass
            pos = self._probe(speed)
            positions.append(pos)
            # Check samples tolerance
            z_positions = [p[2] for p in positions]
            if max(z_positions) - min(z_positions) > samples_tolerance:
                if retries >= samples_retries:
                    raise gcmd.error("Probe samples exceed samples_tolerance")
                gcmd.respond_info("Probe samples exceed tolerance. Retrying...")
                retries += 1
                positions = []
            # Retract
            if len(positions) < sample_count:
                self._move(probexy + [pos[2] + sample_retract_dist], lift_speed)
        if must_notify_multi_probe:
            self.multi_probe_end()
        # Calculate and return result
        if samples_result == 'median':
            return self._calc_median(positions)
        return self._calc_mean(positions)
   
    cmd_PROBE_help = "Probe Z-height at current XY position"
    def cmd_PROBE(self, gcmd):
        pos = self.run_probe(gcmd)
        gcmd.respond_info("Result is z=%.6f" % (pos[2],))
        self.last_z_result = pos[2]
    cmd_QUERY_PROBE_help = "Return the status of the z-probe"
    def cmd_QUERY_PROBE(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        print_time = toolhead.get_last_move_time()
        res = self.mcu_probe.query_endstop(print_time)
        self.last_state = res
        gcmd.respond_info("probe: %s" % (["open", "TRIGGERED"][not not res],))
    def get_status(self, eventtime):
        return {'name': self.name,
                'last_query': self.last_state,
                'last_z_result': self.last_z_result}
    cmd_PROBE_ACCURACY_help = "Probe Z-height accuracy at current XY position"
    def cmd_PROBE_ACCURACY(self, gcmd):
        speed = gcmd.get_float("PROBE_SPEED", self.speed, above=0.)
        lift_speed = self.get_lift_speed(gcmd)
        sample_count = gcmd.get_int("SAMPLES", 10, minval=1)
        sample_retract_dist = gcmd.get_float("SAMPLE_RETRACT_DIST",
                                             self.sample_retract_dist, above=0.)
        toolhead = self.printer.lookup_object('toolhead')
        pos = toolhead.get_position()
        gcmd.respond_info("PROBE_ACCURACY at X:%.3f Y:%.3f Z:%.3f"
                          " (samples=%d retract=%.3f"
                          " speed=%.1f lift_speed=%.1f)\n"
                          % (pos[0], pos[1], pos[2],
                             sample_count, sample_retract_dist,
                             speed, lift_speed))
        # Probe bed sample_count times
        self.multi_probe_begin()
        positions = []
        while len(positions) < sample_count:
            # Probe position
            pos = self._probe(speed)
            positions.append(pos)
            # Retract
            liftpos = [None, None, pos[2] + sample_retract_dist]
            self._move(liftpos, lift_speed)
        self.multi_probe_end()
        # Calculate maximum, minimum and average values
        max_value = max([p[2] for p in positions])
        min_value = min([p[2] for p in positions])
        range_value = max_value - min_value
        avg_value = self._calc_mean(positions)[2]
        median = self._calc_median(positions)[2]
        # calculate the standard deviation
        deviation_sum = 0
        for i in range(len(positions)):
            deviation_sum += pow(positions[i][2] - avg_value, 2.)
        sigma = (deviation_sum / len(positions)) ** 0.5
        # Show information
        gcmd.respond_info(
            "probe accuracy results: maximum %.6f, minimum %.6f, range %.6f, "
            "average %.6f, median %.6f, standard deviation %.6f" % (
            max_value, min_value, range_value, avg_value, median, sigma))
    def probe_calibrate_finalize(self, kin_pos):
        if kin_pos is None:
            return
        z_offset = self.probe_calibrate_z - kin_pos[2]
        self.gcode.respond_info(
            "%s: z_offset: %.3f\n"
            "The SAVE_CONFIG command will update the printer config file\n"
            "with the above and restart the printer." % (self.name, z_offset))
        configfile = self.printer.lookup_object('configfile')
        configfile.set(self.name, 'z_offset', "%.3f" % (z_offset,))
    cmd_PROBE_CALIBRATE_help = "Calibrate the probe's z_offset"
    def cmd_PROBE_CALIBRATE(self, gcmd):
        manual_probe.verify_no_manual_probe(self.printer)
        # Perform initial probe
        lift_speed = self.get_lift_speed(gcmd)
        curpos = self.run_probe(gcmd)
        # Move away from the bed
        self.probe_calibrate_z = curpos[2]
        curpos[2] += 5.
        self._move(curpos, lift_speed)
        # Move the nozzle over the probe point
        curpos[0] += self.x_offset
        curpos[1] += self.y_offset
        self._move(curpos, self.speed)
        # Start manual probe
        manual_probe.ManualProbeHelper(self.printer, gcmd,
                                       self.probe_calibrate_finalize)
    def cmd_Z_OFFSET_APPLY_PROBE(self,gcmd):
        offset = self.gcode_move.get_status()['homing_origin'].z
        configfile = self.printer.lookup_object('configfile')
        if offset == 0:
            self.gcode.respond_info("Nothing to do: Z Offset is 0")
        else:
            new_calibrate = self.z_offset - offset
            self.gcode.respond_info(
                "%s: z_offset: %.3f\n"
                "The SAVE_CONFIG command will update the printer config file\n"
                "with the above and restart the printer."
                % (self.name, new_calibrate))
            configfile.set(self.name, 'z_offset', "%.3f" % (new_calibrate,))
    cmd_Z_OFFSET_APPLY_PROBE_help = "Adjust the probe's z_offset"

# Helper code that can probe a series of points and report the
# position at each point.
class ProbePointsHelper:
    def __init__(self, config, finalize_callback, default_points=None):
        self.printer = config.get_printer()
        self.finalize_callback = finalize_callback
        self.probe_points = default_points
        self.name = config.get_name()
        self.gcode = self.printer.lookup_object('gcode')
        # Read config settings
        if default_points is None or config.get('points', None) is not None:
            self.probe_points = config.getlists('points', seps=(',', '\n'),
                                                parser=float, count=2)
        def_move_z = config.getfloat('horizontal_move_z', 5.)
        self.default_horizontal_move_z = def_move_z
        self.speed = config.getfloat('speed', 50., above=0.)
        self.use_offsets = False
        # Internal probing state
        self.lift_speed = self.speed
        self.probe_offsets = (0., 0., 0.)
        self.results = []
    def minimum_points(self,n):
        if len(self.probe_points) < n:
            raise self.printer.config_error(
                "Need at least %d probe points for %s" % (n, self.name))
    def update_probe_points(self, points, min_points):
        self.probe_points = points
        self.minimum_points(min_points)
    def use_xy_offsets(self, use_offsets):
        self.use_offsets = use_offsets
    def get_lift_speed(self):
        return self.lift_speed
    def _move_next(self):
        toolhead = self.printer.lookup_object('toolhead')
        # Lift toolhead
        speed = self.lift_speed
        if not self.results:
            # Use full speed to first probe position
            speed = self.speed
        toolhead.manual_move([None, None, self.horizontal_move_z], speed)
        # Check if done probing
        if len(self.results) >= len(self.probe_points):
            toolhead.get_last_move_time()
            res = self.finalize_callback(self.probe_offsets, self.results)
            if res != "retry":
                return True
            self.results = []
        # Move to next XY probe point
        nextpos = list(self.probe_points[len(self.results)])
        if self.use_offsets:
            nextpos[0] -= self.probe_offsets[0]
            nextpos[1] -= self.probe_offsets[1]
        toolhead.manual_move(nextpos, self.speed)
        return False

    def fast_probe_oneline(self, gcmd):
        
        probe = self.printer.lookup_object('probe', None)
        
        oneline_points = []
        start_point=list(self.probe_points[len(self.results)])
        end_point = []
        for point in self.probe_points:
            if start_point[1] is point[1]:
                oneline_points.append(point)
        n_count=len(oneline_points)
        if n_count<=1:
            raise self.printer.config_error(
                "Seems the mesh direction is not X, points count on x is %d" % (n_count))
        end_point = list(oneline_points[n_count-1])  
        print(oneline_points)
        print(start_point)
        print(end_point)
        toolhead = self.printer.lookup_object('toolhead')
        if self.use_offsets:
            start_point[0] -= self.probe_offsets[0]
            start_point[1] -= self.probe_offsets[1]
            end_point[0] -= self.probe_offsets[0]
            end_point[1] -= self.probe_offsets[1]
        toolhead.manual_move(start_point, self.speed)
        toolhead.wait_moves()
        toolhead.manual_move(end_point, self.speed)
        ####
        toolhead._flush_lookahead()
        curtime = toolhead.reactor.monotonic()
        est_time =toolhead.mcu.estimated_print_time(curtime)
        line_time = toolhead.print_time-est_time
        start_time = est_time
        x_index = 0
        
        while (not toolhead.special_queuing_state
               or toolhead.print_time >= est_time):
            if not toolhead.can_pause:
                break                
            est_time =toolhead.mcu.estimated_print_time(curtime)    
            
            if (est_time-start_time) >= x_index*line_time/(n_count-1):    
                print(" est:%f,t:%f,dst:%f"%(est_time,(est_time-start_time),x_index*line_time/n_count))
                pos = toolhead.get_position()
                pos[0] = oneline_points[x_index][0]
                pos[1] = oneline_points[x_index][1]
                #pr = probe.mcu_probe.I2C_BD_receive_cmd.send([probe.mcu_probe.oid, "32".encode('utf-8')])
                #intd=int(pr['response'])
                intd=probe.mcu_probe.BD_Sensor_Read(0)
                pos[2]=pos[2]-intd
                probe.gcode.respond_info("probe at %.3f,%.3f is z=%.6f"
                                        % (pos[0], pos[1], pos[2]))
               # return pos[:3]
               # pos = probe.run_probe(gcmd)
                self.results.append(pos)
                x_index += 1;
            curtime = toolhead.reactor.pause(curtime + 0.001)
            
    def fast_probe(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        probe = self.printer.lookup_object('probe', None)
        speed = self.lift_speed
        if not self.results:
            # Use full speed to first probe position
            speed = self.speed
        toolhead.manual_move([None, None, self.horizontal_move_z], speed)
        self.results = []
        while len(self.results) < len(self.probe_points):
            self.fast_probe_oneline(gcmd)
        res = self.finalize_callback(self.probe_offsets, self.results)
        print(self.results)        
        self.results = []
        if res != "retry":
            return True

    def start_probe(self, gcmd):
        manual_probe.verify_no_manual_probe(self.printer)
        # Lookup objects
        probe = self.printer.lookup_object('probe', None)
        method = gcmd.get('METHOD', 'automatic').lower()
        self.results = []
        def_move_z = self.default_horizontal_move_z
        self.horizontal_move_z = gcmd.get_float('HORIZONTAL_MOVE_Z',
                                                def_move_z)
        if probe is None or method != 'automatic':
            # Manual probe
            self.lift_speed = self.speed
            self.probe_offsets = (0., 0., 0.)
            self._manual_probe_start()
            return
        # Perform automatic probing
        self.lift_speed = probe.get_lift_speed(gcmd)
        self.probe_offsets = probe.get_offsets()
        if self.horizontal_move_z < self.probe_offsets[2]:
            raise gcmd.error("horizontal_move_z can't be less than"
                             " probe's z_offset")
        probe.multi_probe_begin()
        
        if gcmd.get_command() == "BED_MESH_CALIBRATE":
             try:
                 if probe.mcu_probe.no_stop_probe is not None:
                     self.fast_probe(gcmd)
                     probe.multi_probe_end()
                     return
             except AttributeError as e:
                 pass
        while 1:
            done = self._move_next()
            if done:
                break
            pos = probe.run_probe(gcmd)
            self.results.append(pos)
        probe.multi_probe_end()
    def _manual_probe_start(self):
        done = self._move_next()
        if not done:
            gcmd = self.gcode.create_gcode_command("", "", {})
            manual_probe.ManualProbeHelper(self.printer, gcmd,
                                           self._manual_probe_finalize)
    def _manual_probe_finalize(self, kin_pos):
        if kin_pos is None:
            return
        self.results.append(kin_pos)
        self._manual_probe_start()

# Calculate a move's accel_t, cruise_t, and cruise_v
def calc_move_time(dist, speed, accel):
    axis_r = 1.
    if dist < 0.:
        axis_r = -1.
        dist = -dist
    if not accel or not dist:
        return axis_r, 0., dist / speed, speed
    max_cruise_v2 = dist * accel
    if max_cruise_v2 < speed**2:
        speed = math.sqrt(max_cruise_v2)
    accel_t = speed / accel
    accel_decel_d = accel_t * speed
    cruise_t = (dist - accel_decel_d) / speed
    return axis_r, accel_t, cruise_t, speed


# I2C BD_SENSOR
# devices connected to an MCU via an virtual i2c bus(2 any gpio)

class MCU_I2C_BD:
    def __init__(self,mcu,   sda_pin,scl_pin, delay_t,home_pose):
        self.mcu = mcu
        #print("MCU_I2C_BD:%s"%mcu)
        self.oid = self.mcu.create_oid()
        # Generate I2C bus config message
        self.config_fmt = (
            "config_I2C_BD oid=%d sda_pin=%s scl_pin=%s delay=%s h_pos=%d"
            % (self.oid, sda_pin,scl_pin, delay_t,home_pose))
        self.cmd_queue = mcu.alloc_command_queue()
        mcu.register_config_callback(self.build_config)
        self.mcu.add_config_cmd(self.config_fmt)
        self.I2C_BD_send_cmd = self.I2C_BD_receive_cmd = None
    def build_config(self):
        #print ("self.config_fmt %s" % self.config_fmt)
        self.I2C_BD_send_cmd = self.mcu.lookup_command(
            "I2C_BD_send oid=%c data=%*s", cq=self.cmd_queue)
        self.I2C_BD_receive_cmd = self.mcu.lookup_query_command(
            "I2C_BD_receive oid=%c data=%*s",
            "I2C_BD_receive_response oid=%c response=%*s",
             oid=self.oid, cq=self.cmd_queue)

    def get_oid(self):
        return self.oid
    def get_mcu(self):
        return self.mcu
    def get_command_queue(self):
        return self.cmd_queue
    def I2C_BD_send(self, data):
        self.I2C_BD_send_cmd.send([self.oid, data.encode('utf-8')])
    def I2C_BD_receive(self,  data):
        return self.I2C_BD_receive_cmd.send([self.oid, data])


# BDsensor wrapper that enables probe specific features
# set this type of sda_pin 2 as virtual endstop
# add new gcode command M102 for BDsensor
class BDsensorEndstopWrapper:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.config = config
        #self.position_endstop = config.getfloat('z_offset')       
        self.position_endstop = config.getfloat('position_endstop',0., minval=0.,below=2.5)
        self.stow_on_each_sample = config.getboolean(
            'deactivate_on_each_sample', True)
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.activate_gcode = gcode_macro.load_template(
            config, 'activate_gcode', '')
        self.deactivate_gcode = gcode_macro.load_template(
            config, 'deactivate_gcode', '')
        self.printer.register_event_handler('klippy:mcu_identify',
                                            self._handle_mcu_identify)
        # Create an "endstop" object to handle the probe pin
        #ppins = self.printer.lookup_object('pins')
       # pin = config.get('sda_pin')
       # pin_params = ppins.lookup_pin(pin, can_invert=True, can_pullup=True)
       # self.mcu_endstop = ppins.setup_pin('pwm', config.get('sda_pin'))
        #self.mcu = pin_params['chip']
        #print(self.mcu)
        # set this type of sda_pin 2 as virtual endstop
        #pin_params['pullup']=2
        #self.mcu_endstop = self.mcu.setup_pin('endstop', pin_params)

        ppins = self.printer.lookup_object('pins')
        #self.mcu_pwm = ppins.setup_pin('pwm', config.get('scl_pin'))

        # Command timing
        self.next_cmd_time = self.action_end_time = 0.
        self.finish_home_complete = self.wait_trigger_complete = None
        # Create an "endstop" object to handle the sensor pin
        
        
        pin = config.get('sda_pin')
        pin_params = ppins.lookup_pin(pin, can_invert=True, can_pullup=True)
        mcu = pin_params['chip']
        sda_pin_num = pin_params['pin']
        self.mcu = mcu
        #print("b2:%s"%mcu)
        pin_params = ppins.lookup_pin(config.get('scl_pin'), can_invert=True, can_pullup=True)
        mcu = pin_params['chip']
        scl_pin_num = pin_params['pin']
        #print("b3:%s"%mcu)
        pin_params['pullup']=2
        self.mcu_endstop = mcu.setup_pin('endstop', pin_params)

        self.oid = self.mcu.create_oid()
        self.cmd_queue = self.mcu.alloc_command_queue()
        # Setup iterative solver
        ffi_main, ffi_lib = chelper.get_ffi()
        self.trapq = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
        self.trapq_append = ffi_lib.trapq_append
        self.trapq_finalize_moves = ffi_lib.trapq_finalize_moves
        self.stepper_kinematics = ffi_main.gc(
            ffi_lib.cartesian_stepper_alloc(b'x'), ffi_lib.free)
        home_pos=self.position_endstop*100
        self.bd_sensor=MCU_I2C_BD(mcu,sda_pin_num,scl_pin_num,config.get('delay'),home_pos)
        #MCU_BD_I2C_from_config(self.mcu,config)
        self.distance=5;
        # Register M102 commands
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command('M102', self.cmd_M102)
        self.no_stop_probe = None
        self.no_stop_probe = config.get('no_stop_probe', None)

        self.I2C_BD_receive_cmd2 = None
        self.gcode_move = self.printer.load_object(config, "gcode_move")
        self.gcode = self.printer.lookup_object('gcode')
        # Wrappers
        self.get_mcu = self.mcu_endstop.get_mcu
        self.add_stepper = self.mcu_endstop.add_stepper
        self.get_steppers = self.mcu_endstop.get_steppers
        #self.home_start = self.mcu_endstop.home_start
        self.home_wait = self.mcu_endstop.home_wait
        #self.query_endstop = self.mcu_endstop.query_endstop
        self.process_m102=0
        self.gcode_que=None
        self.zl=0
        self.bd_value=10.24
        self.x_offset = config.getfloat('x_offset', 0.)
        self.y_offset = config.getfloat('y_offset', 0.)
        self.results = []
        self.finish_home_complete = self.wait_trigger_complete = None
        # multi probes state
        self.multi = 'OFF'
        self.mcu.register_config_callback(self.build_config)
        self.adjust_range=0;
        self.old_count=1000;
        self.homeing=0
        #bd_scheduler = sched.scheduler(time.time, time.sleep)
        #bd_scheduler.enter(1, 1, self.BD_loop, (bd_scheduler,))
        #bd_scheduler.run()
        #Timer(2, self.BD_loop, ()).start()
        self.reactor = self.printer.get_reactor()
        #self.bd_update_timer = self.reactor.register_timer(
        #    self.bd_update_event)
        #self.reactor.update_timer(self.bd_update_timer, self.reactor.NOW)
        self.status_dis = None
        try:
            status_dis=self.printer.lookup_object('display_status')
        except Exception as e:
            pass

    def z_live_adjust(self):
        print ("z_live_adjust %d" % self.adjust_range)
        if self.adjust_range<=0 or self.adjust_range > 40:
            return
        self.toolhead = self.printer.lookup_object('toolhead')
        phoming = self.printer.lookup_object('homing')
       # print ("homing_status %d" % phoming.homing_status)
       # if phoming.homing_status == 1:
        #    return
        #x, y, z, e = self.toolhead.get_position()
        z=self.gcode_move.last_position[2]
        print ("z %.4f" % z)
        if z is None:
            return
        if z*10>self.adjust_range:
            return
        if self.bd_value < 0:
            return
        if self.bd_value>10.15:
            return;
        if abs(z-self.bd_value)<=0.01:
            return;
        print ("z_post:%.4f" % z)
        print ("bd_value:%.4f" % self.bd_value)
        #self.toolhead.wait_moves()
        kin = self.toolhead.get_kinematics()
        distance = 0.5#gcmd.get_float('DISTANCE')
        speed = 7#gcmd.get_float('VELOCITY', above=0.)
        accel = 2000#gcmd.get_float('ACCEL', 0., minval=0.)
        ajust_len=-0.01
        #if z > self.bd_value:
        #    ajust_len = (z-self.bd_value)
        #else:
        #ajust_len = z+z-self.bd_value
        self.zl= self.zl+0.1
        ajust_len =  z-self.bd_value
        print ("ajust_len:%.4f" % ajust_len)
        #if ajust_len<0:
        #    return
        dir=1
        if ajust_len>0.000001:
            dir=0
        delay=1000000
        steps_per_mm = 1.0/stepper.get_step_dist()
        for stepper in kin.get_steppers():
            if stepper.is_active_axis('z'):
                cmd_fmt = (
                "%u %u %u %u\0"
                % (dir,steps_per_mm, delay,stepper.get_oid()))
                pr=self.Z_Move_Live_cmd.send([self.oid,
                    cmd_fmt.encode('utf-8')])
                print("get:%s " %pr['return_set'])
           #self.toolhead.manual_move([None, None, ajust_len], speed)

    #def bd_update_event(self, eventtime):
    #    if self.gcode_que is not None:
    #        self.process_M102(self.gcode_que)
    #        self.gcode_que=None
        #self.z_live_adjust()
        #self.Z_Move_Live_cmd.send([self.oid,
        #            ("d 0\0" ).encode('utf-8')])
    #    return eventtime + BD_TIMER

    def build_config(self):
        self.I2C_BD_receive_cmd = self.mcu.lookup_query_command(
            "I2C_BD_receive oid=%c data=%*s",
            "I2C_BD_receive_response oid=%c response=%*s",
            oid=self.oid, cq=self.cmd_queue)
        #self.I2C_BD_receive_cmd2 = self.mcu.lookup_query_command(
        #    "I2C_BD_receive2 oid=%c data=%*s",
        #    "I2C_BD_receive2_response oid=%c response=%*s",
         #   oid=self.oid, cq=self.cmd_queue)

        self.Z_Move_Live_cmd = self.mcu.lookup_query_command(
            "Z_Move_Live oid=%c data=%*s",
            "Z_Move_Live_response oid=%c return_set=%*s",
            oid=self.oid, cq=self.cmd_queue)
        self.mcu.register_response(self._handle_BD_Update,
                                    "BD_Update", self.bd_sensor.oid)
        self.mcu.register_response(self._handle_probe_Update,
                                    "X_probe_Update", self.bd_sensor.oid)
    def _handle_BD_Update(self, params):
        #print("_handle_BD_Update :%s " %params['distance_val'])
        try:
            self.bd_value=int(params['distance_val'])/100.00
            if self.status_dis is not None:
                strd=str(self.bd_value)+"mm"
            if self.bd_value == 10.24:
                strd="BDs:ConnectErr"
            if self.bd_value == 3.9:
               strd="BDs:Out Range"
            self.status_dis.message=strd
        except ValueError as e:
            pass
        #else:
            #print (" handle self.bd_value %.4f" % self.bd_value)
    def _handle_probe_Update(self, params):
        print("_handle_probe_Update:%s " %params['distance_val'])
        #print ("split :%s " %params['distance_val'].split(b' '))
        count=int(params['distance_val'].split(b' ')[1])
        print(len(self.results))

        self.old_count=count
        print ("split:%s " %params['distance_val'].split(b' '))
        try:
            self.results.append(int(params['distance_val'].split(b' ')[0]))
        except ValueError as e:
            pass
    def manual_move2(self, stepper, dist, speed, accel=0.):
         self.toolhead = self.printer.lookup_object('toolhead')
         self.toolhead.flush_step_generation()
         prev_sk = stepper.set_stepper_kinematics(self.stepper_kinematics)
         prev_trapq = stepper.set_trapq(self.trapq)
         stepper.set_position((0., 0., 0.))
         axis_r, accel_t,cruise_t,cruise_v=calc_move_time(dist, speed, accel)
         print_time = self.toolhead.get_last_move_time()
         self.trapq_append(self.trapq, print_time, accel_t, cruise_t, accel_t,
                           0., 0., 0., axis_r, 0., 0., 0., cruise_v, accel)
         print_time = print_time + accel_t + cruise_t + accel_t
         stepper.generate_steps(print_time)
         self.trapq_finalize_moves(self.trapq, print_time + 99999.9)
         stepper.set_trapq(prev_trapq)
         stepper.set_stepper_kinematics(prev_sk)
         self.toolhead.note_kinematic_activity(print_time)
         #self.toolhead.dwell(accel_t + cruise_t + accel_t)

    def _force_enable(self,stepper):
        self.toolhead = self.printer.lookup_object('toolhead')
        print_time = self.toolhead.get_last_move_time()
        stepper_enable = self.printer.lookup_object('stepper_enable')
        enable = stepper_enable.lookup_enable(stepper.get_name())
        was_enable = enable.is_motor_enabled()
        STALL_TIME = 0.100
        if not was_enable:
            enable.motor_enable(print_time)
            self.toolhead.dwell(STALL_TIME)
        return was_enable

    def manual_move(self, stepper, dist, speed, accel=0.):
         self.toolhead = self.printer.lookup_object('toolhead')
         self.toolhead.flush_step_generation()
         prev_sk = stepper.set_stepper_kinematics(self.stepper_kinematics)
         prev_trapq = stepper.set_trapq(self.trapq)
         stepper.set_position((0., 0., 0.))
         axis_r, accel_t,cruise_t,cruise_v=calc_move_time(dist, speed, accel)
         print_time = self.toolhead.get_last_move_time()
         self.trapq_append(self.trapq, print_time, accel_t, cruise_t, accel_t,
                           0., 0., 0., axis_r, 0., 0., 0., cruise_v, accel)
         print_time = print_time + accel_t + cruise_t + accel_t
         stepper.generate_steps(print_time)
         self.trapq_finalize_moves(self.trapq, print_time + 99999.9)
         stepper.set_trapq(prev_trapq)
         stepper.set_stepper_kinematics(prev_sk)
         self.toolhead.note_kinematic_activity(print_time)
         self.toolhead.dwell(accel_t + cruise_t + accel_t)

    def cmd_M102(self, gcmd, wait=False):
         #self.gcode_que=gcmd
         self.process_M102(gcmd)
    def sync_motor_probe(self):
        try:
            if self.no_stop_probe is None:
                return
        except AttributeError as e:
            pass    
            return
        step_time=100
        self.toolhead = self.printer.lookup_object('toolhead')
        bedmesh = self.printer.lookup_object('bed_mesh', None)
        self.min_x, self.min_y = bedmesh.bmc.orig_config['mesh_min']
        self.max_x, self.max_y = bedmesh.bmc.orig_config['mesh_max']
        x_count=bedmesh.bmc.orig_config['x_count']
        kin = self.toolhead.get_kinematics()
        for stepper in kin.get_steppers():
            if stepper.get_name()=='stepper_x':
                steps_per_mm = 1.0/stepper.get_step_dist()
                x=self.gcode_move.last_position[0]
                stepper._query_mcu_position()
                invert_dir, orig_invert_dir = stepper.get_dir_inverted()
                print("invert_dir:%d,%d" % (invert_dir,orig_invert_dir))
                print("x ==%.f %.f  %.f steps_per_mm:%d,%u"%
                    (self.min_x,self.max_x,x_count,steps_per_mm,
                    stepper.get_oid()))
                print("kinematics:%s" %
                    self.config.getsection('printer').get('kinematics'))
                bedmesh = self.printer.lookup_object('bed_mesh', None)
                print_type=0 # default is 'cartesian'
                if 'delta' ==(
                  self.config.getsection('printer').get('kinematics')):
                    print_type=2
                if 'corexy' ==(
                  self.config.getsection('printer').get('kinematics')):
                    print_type=1

                x=x*1000
                
                pr=self.Z_Move_Live_cmd.send([self.oid, 
                    ("3 %u\0" % invert_dir).encode('utf-8')])
                pr=self.Z_Move_Live_cmd.send([self.oid,
                    ("7 %d\0" %
                    (self.min_x-self.x_offset)).encode('utf-8')])
                pr=self.Z_Move_Live_cmd.send([self.oid,
                    ("8 %d\0" % (self.max_x-self.x_offset)).encode('utf-8')])
                pr=self.Z_Move_Live_cmd.send([self.oid,
                    ("9 %d\0" % x_count).encode('utf-8')])
                pr=self.Z_Move_Live_cmd.send([self.oid,
                    ("a %d\0"   % x).encode('utf-8')])
                pr=self.Z_Move_Live_cmd.send([self.oid,
                    ("b %d\0"  % steps_per_mm).encode('utf-8')])
                pr=self.Z_Move_Live_cmd.send([self.oid,
                    ("c %u\0"  % stepper.get_oid()).encode('utf-8')])
                pr=self.Z_Move_Live_cmd.send([self.oid,
                    ("d 0\0" ).encode('utf-8')])
                pr=self.Z_Move_Live_cmd.send([self.oid,
                    ("e %d\0"  % print_type).encode('utf-8')])

                self.results=[]
                print("xget:%s " %pr['return_set'])
            if stepper.get_name()=='stepper_y':
                steps_per_mm = 1.0/stepper.get_step_dist()
                invert_dir, orig_invert_dir = stepper.get_dir_inverted()
                y=self.gcode_move.last_position[1]
                #stepper._query_mcu_position()
                print("y per_mm:%d,%u"%(steps_per_mm,stepper.get_oid()))
                #invert_dir, orig_invert_dir = stepper.get_dir_inverted()
                #bedmesh = self.printer.lookup_object('bed_mesh', None)
                #bedmesh.bmc.orig_config['mesh_min']
                y=y*1000

                pr=self.Z_Move_Live_cmd.send([self.oid,
                    ("f %d\0"   % y).encode('utf-8')])
                pr=self.Z_Move_Live_cmd.send([self.oid,
                    ("g %d\0"  % steps_per_mm).encode('utf-8')])
                pr=self.Z_Move_Live_cmd.send([self.oid, 
                    ("h %u\0" % invert_dir).encode('utf-8')])    
                pr=self.Z_Move_Live_cmd.send([self.oid,
                    ("i %u\0"  % stepper.get_oid()).encode('utf-8')])
        self.bd_sensor.I2C_BD_send("1018")#1018// finish reading
        pr=self.Z_Move_Live_cmd.send([self.oid,
                    ("j 98000\0").encode('utf-8')])
    def BD_Sensor_Read(self,fore_r):
        if fore_r > 0:
            self.bd_sensor.I2C_BD_send("1018")#1015   read distance data
        pr = self.I2C_BD_receive_cmd.send([self.oid, "32".encode('utf-8')])
        intr = int(pr['response'])
        if intr >= 1024:
            pr = self.I2C_BD_receive_cmd.send([self.oid, "32".encode('utf-8')])
            intr = int(pr['response'])
        self.bd_value=intr/100.00
        if fore_r == 0:
            if self.bd_value >= 10.24:
                self.gcode.respond_info("Bed Distance Sensor data error:%.2f" % (self.bd_value))
                raise self.printer.command_error("Bed Distance Sensor data error:%.2f" % (self.bd_value))
            elif self.bd_value > 3.8:
                self.gcode.respond_info("Bed Distance Sensor, out of range.:%.2f " % (self.bd_value))
                raise self.printer.command_error("Bed Distance Sensor, out of range.:%.2f " % (self.bd_value))
        elif fore_r == 2:
            if self.bd_value >= 10.24:
                self.gcode.respond_info("Bed Distance Sensor data error:%.2f" % (self.bd_value))
                raise self.printer.command_error("Bed Distance Sensor data error:%.2f" % (self.bd_value))

        return self.bd_value
    def process_M102(self, gcmd):
        self.process_m102=1
        #print(gcmd)
        #self.reactor.update_timer(self.bd_update_timer, self.reactor.NOW)
        #self.reactor.update_timer(self.bd_update_timer, self.reactor.NOW)
        try:
            CMD_BD = gcmd.get_int('S', None)
        except Exception as e:
            pass
            return
        self.toolhead = self.printer.lookup_object('toolhead')
        if CMD_BD == -6:
            self.gcode.respond_info("Calibrating from 0.0mm to 3.9mm, don't power off the printer")
            kin = self.toolhead.get_kinematics()
            self.bd_sensor.I2C_BD_send("1019")
            self.bd_sensor.I2C_BD_send("1019")
            #distance = 0.5#gcmd.get_float('DISTANCE')
            speed = 5#gcmd.get_float('VELOCITY', above=0.)
            accel = 1000#gcmd.get_float('ACCEL', 0., minval=0.)
            self.distance=0.1
            for stepper in kin.get_steppers():
                #if stepper.is_active_axis('z'):
                self._force_enable(stepper)
                self.toolhead.wait_moves()
            ncount=0
            self.gcode.respond_info("Please Waiting... ")
            self.toolhead.dwell(0.8)
            while 1:
                self.bd_sensor.I2C_BD_send(str(ncount))
                self.bd_sensor.I2C_BD_send(str(ncount))
                self.bd_sensor.I2C_BD_send(str(ncount))
                self.bd_sensor.I2C_BD_send(str(ncount))
                self.toolhead.dwell(0.2)
                for stepper in kin.get_steppers():
                    if stepper.is_active_axis('z'):
                       # self._force_enable(stepper)
                        self.manual_move(stepper, self.distance, speed,accel)
                self.toolhead.wait_moves()
                self.toolhead.dwell(0.2)
                ncount=ncount+1
                    
                if ncount>=40:
                    self.bd_sensor.I2C_BD_send("1021")
                    self.toolhead.dwell(1)
                    self.gcode.respond_info("Calibrate Finished!")
                    self.gcode.respond_info("You can send M102 S-5 to check the calibration data")
                    break
        elif  CMD_BD == -5:
            self.bd_sensor.I2C_BD_send("1017")#tart read raw calibrate data
            self.bd_sensor.I2C_BD_send("1017")
            ncount1=0
            while 1:
                pr=self.I2C_BD_receive_cmd.send([self.oid,"3".encode('utf-8')])
                intd=int(pr['response'])
                strd=str(intd)
                gcmd.respond_raw(strd)
                if ncount1 <= 3 and intd > 550 :
                    if intd>=1015:
                        gcmd.respond_raw("BDSensor mounted too close or too high!  0.4mm to 2.4mm from BED at zero position is recommended")
                        raise self.printer.command_error("BDSensor mounted too close or too high!" % intd)
                        break
                    gcmd.respond_raw("BDSensor mounted too high!  0.4mm to 2.4mm from BED at zero position is recommended")
                    break
                if intd < 45 :
                    gcmd.respond_raw("BDSensor mounted too close! please mount the BDsensor 0.2~0.4mm higher")
                    break
                self.toolhead.dwell(0.1)
                ncount1=ncount1+1
                if ncount1>=40:
                    break
        elif  CMD_BD == -1:
            self.bd_sensor.I2C_BD_send("1016")#1016 // // read sensor version
            self.bd_sensor.I2C_BD_send("1016")
            ncount1=0
            x=[]
            while 1:
                pr=self.I2C_BD_receive_cmd.send([self.oid,"3".encode('utf-8')])
              #  print"params:%s" % pr['response']
                intd=int(pr['response'])
                if intd>127:
                    intd=127
                if intd<0x20:
                    intd=0x20
                x.append(intd)
                self.toolhead.dwell(0.1)
                ncount1=ncount1+1
                if ncount1>=20:
                    self.bd_sensor.I2C_BD_send("1018")#1018// finish reading
                    res = ''.join(map(chr, x))
                    gcmd.respond_raw(res)
                    break
        elif  CMD_BD == -2:# gcode M102 S-2 read distance data
            #self.bd_sensor.I2C_BD_send("1015")#1015   read distance data
            #pr = self.I2C_BD_receive_cmd.send([self.oid, "32".encode('utf-8')])
            #self.bd_value=int(pr['response'])/100.00
            self.bd_value=self.BD_Sensor_Read(1)
            strd=str(self.bd_value)+"mm"
            if self.bd_value == 10.24:
                strd="BDsensor:Connection Error"
            elif self.bd_value >= 3.9:
                strd="BDsensor:Out of measure Range"
            gcmd.respond_raw(strd)
        elif  CMD_BD ==-7:# gcode M102 Sx
            #self.bd_sensor.I2C_BD_send("1022")
            step_time=100
            self.toolhead = self.printer.lookup_object('toolhead')
            bedmesh = self.printer.lookup_object('bed_mesh', None)
            self.min_x, self.min_y = bedmesh.bmc.orig_config['mesh_min']
            self.max_x, self.max_y = bedmesh.bmc.orig_config['mesh_max']
            x_count=bedmesh.bmc.orig_config['x_count']
            kin = self.toolhead.get_kinematics()
            for stepper in kin.get_steppers():
                if stepper.get_name()=='stepper_x':
                    steps_per_mm = 1.0/stepper.get_step_dist()
                    x=self.gcode_move.last_position[0]
                    stepper._query_mcu_position()
                    invert_dir, orig_invert_dir = stepper.get_dir_inverted()
                    print("invert_dir:%d,%d" % (invert_dir,orig_invert_dir))
                    print("x ==%.f %.f  %.f steps_per_mm:%d,%u"%
                        (self.min_x,self.max_x,x_count,steps_per_mm,
                        stepper.get_oid()))
                    print("kinematics:%s" %
                        self.config.getsection('printer').get('kinematics'))
                    bedmesh = self.printer.lookup_object('bed_mesh', None)
                    print_type=0 # default is 'cartesian'
                    if 'delta' ==(
                      self.config.getsection('printer').get('kinematics')):
                        print_type=2
                    if 'corexy' ==(
                      self.config.getsection('printer').get('kinematics')):
                        print_type=1

                    x=x*1000
                    
                    pr=self.Z_Move_Live_cmd.send([self.oid, 
                        ("3 %u\0" % invert_dir).encode('utf-8')])
                    pr=self.Z_Move_Live_cmd.send([self.oid,
                        ("7 %d\0" %
                        (self.min_x-self.x_offset)).encode('utf-8')])
                    pr=self.Z_Move_Live_cmd.send([self.oid,
                        ("8 %d\0" % (self.max_x-self.x_offset)).encode('utf-8')])
                    pr=self.Z_Move_Live_cmd.send([self.oid,
                        ("9 %d\0" % x_count).encode('utf-8')])
                    pr=self.Z_Move_Live_cmd.send([self.oid,
                        ("a %d\0"   % x).encode('utf-8')])
                    pr=self.Z_Move_Live_cmd.send([self.oid,
                        ("b %d\0"  % steps_per_mm).encode('utf-8')])
                    pr=self.Z_Move_Live_cmd.send([self.oid,
                        ("c %u\0"  % stepper.get_oid()).encode('utf-8')])
                    pr=self.Z_Move_Live_cmd.send([self.oid,
                        ("d 0\0" ).encode('utf-8')])
                    pr=self.Z_Move_Live_cmd.send([self.oid,
                        ("e %d\0"  % print_type).encode('utf-8')])

                    self.results=[]
                    print("xget:%s " %pr['return_set'])
                if stepper.get_name()=='stepper_y':
                    steps_per_mm = 1.0/stepper.get_step_dist()
                    invert_dir, orig_invert_dir = stepper.get_dir_inverted()
                    y=self.gcode_move.last_position[1]
                    #stepper._query_mcu_position()
                    print("y per_mm:%d,%u"%(steps_per_mm,stepper.get_oid()))
                    #invert_dir, orig_invert_dir = stepper.get_dir_inverted()
                    #bedmesh = self.printer.lookup_object('bed_mesh', None)
                    #bedmesh.bmc.orig_config['mesh_min']
                    y=y*1000

                    pr=self.Z_Move_Live_cmd.send([self.oid,
                        ("f %d\0"   % y).encode('utf-8')])
                    pr=self.Z_Move_Live_cmd.send([self.oid,
                        ("g %d\0"  % steps_per_mm).encode('utf-8')])
                    pr=self.Z_Move_Live_cmd.send([self.oid, 
                        ("h %u\0" % invert_dir).encode('utf-8')])    
                    pr=self.Z_Move_Live_cmd.send([self.oid,
                        ("i %u\0"  % stepper.get_oid()).encode('utf-8')])

                    self.results=[]
                    print("yget:%s " %pr['return_set'])
                    #print(cmd_fmt)
            #self.bd_sensor.I2C_BD_send("1018")#1018// finish reading
        elif  CMD_BD ==-8:
            self.bd_sensor.I2C_BD_send("1022") #reboot sensor
        else:
            return
        self.bd_sensor.I2C_BD_send("1018")#1018// finish reading
        self.bd_sensor.I2C_BD_send("1018")
        #self.process_m102=0
    def _handle_mcu_identify(self):
        #print("BD _handle_mcu_identify")
        kin = self.printer.lookup_object('toolhead').get_kinematics()
        for stepper in kin.get_steppers():
            if stepper.is_active_axis('z'):
                self.add_stepper(stepper)
    def raise_probe(self):
        print("BD raise_probe")
        return
        self.toolhead = self.printer.lookup_object('toolhead')
        start_pos = self.toolhead.get_position()
        self.deactivate_gcode.run_gcode_from_command()
        if self.toolhead.get_position()[:3] != start_pos[:3]:
            raise self.printer.command_error(
                "Toolhead moved during probe activate_gcode script")
    def lower_probe(self):
        print("BD lower_probe0")
        return
        self.toolhead = self.printer.lookup_object('toolhead')
        start_pos = self.toolhead.get_position()
        self.activate_gcode.run_gcode_from_command()
        if self.toolhead.get_position()[:3] != start_pos[:3]:
            raise self.printer.command_error(
                "Toolhead moved during probe deactivate_gcode script")

    def query_endstop(self, print_time):
        print("query Z endstop")
        #params = self.mcu_endstop.query_endstop(print_time)
        #print(params)
        #self.bd_sensor.I2C_BD_send("1018")#1015   read distance data
        #pr = self.I2C_BD_receive_cmd.send([self.oid, "32".encode('utf-8')])
        self.bd_value=self.BD_Sensor_Read(2)
        params = 1 #trigered
        if self.bd_value > self.position_endstop:# open
           params=0
        return params

    def home_start(self, print_time, sample_time, sample_count, rest_time,
                   triggered=True):
        print("BD home_start")
        self.homeing=1
        ENDSTOP_REST_TIME = .001
        rest_time = min(rest_time, ENDSTOP_REST_TIME)
        self.finish_home_complete = self.mcu_endstop.home_start(
            print_time, sample_time, sample_count, rest_time, triggered)
        # Schedule wait_for_trigger callback
        r = self.printer.get_reactor()
        self.wait_trigger_complete = r.register_callback(self.wait_for_trigger)   
        return self.finish_home_complete

    def wait_for_trigger(self, eventtime):
        #print("BD wait_for_trigger") 
        self.BD_Sensor_Read(2)
       # home_pos=self.position_endstop*100
       # pr=self.Z_Move_Live_cmd.send([self.oid,
       #         ("m %d\0" % home_pos).encode('utf-8')])
        pr=self.Z_Move_Live_cmd.send([self.oid,
                ("k 5\0").encode('utf-8')])
        self.finish_home_complete.wait()
        if self.multi == 'OFF':
            self.raise_probe()
    def multi_probe_begin(self):
        print("BD multi_probe_begin")
        #self.bd_sensor.I2C_BD_send("1022")
        if self.stow_on_each_sample:
            return
        self.multi = 'FIRST'

    def multi_probe_end(self):
        print("BD multi_probe_end")
        self.bd_sensor.I2C_BD_send("1018")
        if self.homeing==1:           
            self.bd_value=self.BD_Sensor_Read(0)
            self.toolhead = self.printer.lookup_object('toolhead')
            self.toolhead.wait_moves()
            time.sleep(0.004)
            self.gcode.run_script_from_command("G92 Z%.3f" % self.bd_value)
            self.gcode.respond_info("The actually triggered position of Z is %.3f mm"%self.bd_value)
            
        #else:#set x stepper oid=0 to recovery normal timer
         #   pr=self.Z_Move_Live_cmd.send([self.oid,
         #           ("j 0\0").encode('utf-8')])
        self.homeing=0
        if self.stow_on_each_sample:
            return
        self.raise_probe()
        self.multi = 'OFF'
    def probe_prepare(self, hmove):
        print("BD probe_prepare")
        if self.multi == 'OFF' or self.multi == 'FIRST':
            self.lower_probe()
            if self.multi == 'FIRST':
                self.multi = 'ON'
    def probe_finish(self, hmove):
        print("BD probe_finish")
        self.bd_sensor.I2C_BD_send("1018")
        if self.multi == 'OFF':
            self.raise_probe()
       # pr=self.Z_Move_Live_cmd.send([self.oid,
        #            ("j 0\0").encode('utf-8')])   
        pr=self.Z_Move_Live_cmd.send([self.oid,
                ("k 100\0").encode('utf-8')])
    def get_position_endstop(self):
        #print("BD get_position_endstop")
        return self.position_endstop

def load_config(config):
    bdl=BDsensorEndstopWrapper(config)
    config.get_printer().add_object('probe', PrinterProbe(config, bdl))
    return bdl
