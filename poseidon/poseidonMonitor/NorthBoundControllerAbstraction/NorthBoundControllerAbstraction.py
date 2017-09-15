#
#   Copyright (c) 2016 In-Q-Tel, Inc, All Rights Reserved.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
'''
Created on 17 May 2016
@author: dgrossman
'''
import hashlib
import json
import logging
import Queue
import threading
import time
import types
from functools import partial
from os import getenv
from collections import defaultdict

from poseidon.baseClasses.Monitor_Action_Base import Monitor_Action_Base
from poseidon.baseClasses.Monitor_Helper_Base import Monitor_Helper_Base
from poseidon.poseidonMonitor.NorthBoundControllerAbstraction.proxy.bcf.bcf import BcfProxy

module_logger = logging.getLogger(__name__)


class NorthBoundControllerAbstraction(Monitor_Action_Base):
    ''' handle abstracting poseidon from the controllers '''

    def __init__(self):
        super(NorthBoundControllerAbstraction, self).__init__()
        self.logger = module_logger
        self.mod_name = self.__class__.__name__
        self.config_section_name = self.mod_name


class Update_Switch_State(Monitor_Helper_Base):
    ''' handle periodic process, determine if switch state updated '''

    def __init__(self):
        super(Update_Switch_State, self).__init__()
        self.logger = module_logger
        self.mod_name = self.__class__.__name__
        self.retval = {}
        self.times = 0
        self.owner = None
        self.controller = {}
        self.controller['URI'] = None
        self.controller['USER'] = None
        self.controller['PASS'] = None
        self.bcf = None
        self.first_time = True
        self.endpoint_states = defaultdict(dict)
        self.m_queue = Queue.Queue()

    def return_endpoint_state(self):
        return self.endpoint_states

    def first_run(self):
        ''' do some pre-run setup/configuration '''
        if self.configured:
            self.controller['URI'] = str(
                self.mod_configuration['controller_uri'])
            self.controller['USER'] = str(
                self.mod_configuration['controller_user'])
            self.controller['PASS'] = str(
                self.mod_configuration['controller_pass'])

            myauth = {}
            myauth['password'] = self.controller['PASS']
            myauth['user'] = self.controller['USER']
            try:
                self.bcf = BcfProxy(self.controller['URI'], auth=myauth)
            except BaseException:
                self.logger.error(
                    'BcfProxy coult not connect to {0}'.format(
                        self.controller['URI']))
        else:
            pass

    @staticmethod
    def make_hash(item):
        ''' hash the metadata in a sane way'''
        h = hashlib.new('ripemd160')
        pre_h = str()
        post_h = None
        # nodhcp -> dhcp withname makes different hashes
        # {u'tenant': u'FLOORPLATE', u'mac': u'ac:87:a3:2b:7f:12', u'segment': u'prod', u'name': None, u'ip-address': u'10.179.0.100'}}^
        # {u'tenant': u'FLOORPLATE', u'mac': u'ac:87:a3:2b:7f:12', u'segment': u'prod', u'name': u'demo-laptop', u'ip-address': u'10.179.0.100'}}
        # ^^^ make different hashes if name is included
        # for word in ['tenant', 'mac', 'segment', 'name', 'ip-address']:

        for word in ['tenant', 'mac', 'segment', 'ip-address']:
            pre_h = pre_h + str(item.get(str(word), 'missing'))
        h.update(pre_h)
        post_h = h.hexdigest()
        return post_h

    def handle_item(self, item):
        ''' perform an action based on rabbit item'''
        self.logger.debug('handle_item: {0}:{1}'.format(item, type(item)))
        itype = item[0]
        ivalue = item[1]
        ivalue = json.loads(ivalue)
        self.logger.debug(
            'handle_item: ivalue json: {0}:{1}'.format(ivalue, type(ivalue)))

        if itype == 'poseidon.action.start_monitor':
            for my_hash, my_dict in ivalue.iteritems():
                if my_hash in self.new_endpoints:
                    v = self.new_endpoints.pop(my_hash)
                    self.logger.debug(
                        'removed {0} from new_endpoints'.format(v))
                else:
                    self.logger.debug('could not find {0} in {1}'.format(
                        my_hash, self.new_endpoints))

                self.logger.debug(
                    'mirroring :{0}'.format(my_dict['ip-address']))
                self.logger.debug(
                    'mirroring[{0}]={1}'.format(my_hash, my_dict))
                self.bcf.mirror_ip(my_dict['ip-address'])
                self.endpoint_states[my_hash]['state'] = 'MIRRORING'
                #self.mirroring[my_hash] = my_dict

        if itype == 'poseidon.action.endpoint_shutdown':
            self.logger.debug(
                'endpoint_shutdown:{0}:{1}'.format(ivalue, type(ivalue)))
            for my_hash, my_dict in ivalue.iteritems():
                bad_ip = my_dict.get('ip-address')
                if bad_ip is not None:
                    self.logger.debug(
                        '****** shutdown {0}:{1}'.format(bad_ip, ivalue))
                    self.bcf.shutdown_ip(bad_ip)
                    self.endpoint_states[my_hash]['state'] = 'SHUTDOWN'
                    #self.shutdown[my_hash] = my_dict

        if itype == 'poseidon.action.stop_monitor':
            self.logger.debug('stop_monitor:{0}:{1}'.format(itype, ivalue))
            for my_hash, my_dict in ivalue.iteritems():
                self.logger.debug('stop_monitor_dict:{0}'.format(my_dict))
                my_ip = my_dict.get('ip-address')
                if my_ip is not None:
                    self.logger.debug('***** shutting down {0}'.format(my_ip))
                    self.bcf.unmirror_ip(my_ip)
                    if self.endpoint_states[my_hash]['state'] == 'MIRRORING':
                        self.endpoint_states[my_hahs]['state'] = 'KNOWN'

                    # if my_hash in self.mirroring:
                    #    self.mirroring.pop(my_hash)

    def make_endpoint_dict(self, hash, state, data):
        self.endpoint_states[hash]['state'] = state
        self.endpoint_states[hash]['endpoint'] = data

    def change_endpoint_state(self, hash, new_state):
        self.endpoint_states[hash]['state'] = new_state

    def find_new_machines(self, machines):
        '''parse switch structure to find new machines added to network
        since last call'''
        if self.first_time:
            self.first_time = False
            # TODO db call to see if really need to run things
            for machine in machines:
                h = self.make_hash(machine)
                module_logger.critical(
                    'adding address to known systems {0}'.format(machine))
                self.make_endpoint_dict(h, 'KNOWN', machine)
                #self.prev_endpoints[h] = machine
        else:
            for machine in machines:
                h = self.make_hash(machine)
                if h not in self.endpoint_states:
                    module_logger.critical(
                        '***** detected new address {0}'.format(machine))
                    self.make_endpoint_dict(h, 'UNKNOWN', machine)
                    #self.new_endpoints[h] = machine

    def print_endpoint_state(self):
        def same_old(logger, state, letter, endpoint_states):
            logger.debug('*******{0}*********'.format(state))

            out_flag = False
            for my_hash in endpoint_states.keys():
                my_dict = endpoint_states[my_hash]
                if my_dict['state'] == state:
                    out_flag = True
                    logger.debug('{0}:{1}:{2}'.format(
                        letter, my_hash, my_dict['endpoint']))
            if not out_flag:
                logger.debug('None')

        states = [('K', 'KNOWN'), ('U', 'UNKNOWN'), ('M', 'MIRRORING'),
                  ('S', 'SHUTDOWN'), ('R', 'REINVESTIGATING')]

        for l, s in states:
            same_old(self.logger, s, l, self.endpoint_states)

        self.logger.debug('****************')

    def update_endpoint_state(self):
        '''Handles Get requests'''
        self.retval['service'] = self.owner.mod_name + ':' + self.mod_name
        self.retval['times'] = self.times
        self.retval['machines'] = None
        self.retval['resp'] = 'bad'

        current = None
        parsed = None
        machines = {}

        try:
            current = self.bcf.get_endpoints()
            parsed = self.bcf.format_endpoints(current)
            machines = parsed
        except BaseException:
            self.logger.error(
                'Could not establish connection to {0}.'.format(
                    self.controller['URI']))
            self.retval['controller'] = 'Could not establish connection to {0}.'.format(
                self.controller['URI'])

        self.logger.debug('MACHINES:{0}'.format(machines))
        self.find_new_machines(machines)

        self.print_endpoint_state()

        self.retval['machines'] = parsed
        self.retval['resp'] = 'ok'

        self.times = self.times + 1

        return json.dumps(self.retval)


controller_interface = NorthBoundControllerAbstraction()
controller_interface.add_endpoint('Update_Switch_State', Update_Switch_State)
