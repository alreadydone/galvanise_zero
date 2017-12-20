import os
import sys
import time
import shutil

import json

from twisted.internet import reactor

from ggplib.util import log
from ggplib.db import lookup

from ggplearn.util import attrutil, runprocs
from ggplearn.util.broker import Broker, ServerFactory

from ggplearn import msgdefs
from ggplearn.nn import network


def critical_error(msg):
    log.critical(msg)
    reactor.stop()
    sys.exit(1)


def default_conf():
    conf = msgdefs.ServerConfig()

    conf.port = 9000
    conf.game = "breakthrough"
    conf.current_step = 1

    conf.score_network_size = "small"
    conf.policy_network_size = "small"

    conf.generation_prefix = "v3_"
    conf.store_path = os.path.join(os.environ["GGPLEARN_PATH"], "data", "breakthrough", "v3")

    # generation set on server
    conf.player_select_conf = msgdefs.PolicyPlayerConf(verbose=False,
                                                       choose_exponential_scale=0.3)

    conf.player_policy_conf = msgdefs.PUCTPlayerConf(name="policy_puct",
                                                     verbose=False,
                                                     playouts_per_iteration=800,
                                                     playouts_per_iteration_noop=0,
                                                     expand_root=100,
                                                     dirichlet_noise_alpha=-1,
                                                     cpuct_constant_first_4=3.0,
                                                     cpuct_constant_after_4=0.75,
                                                     choose="choose_converge")

    conf.player_score_conf = msgdefs.PolicyPlayerConf(verbose=False,
                                                      choose_exponential_scale=-1)
    conf.generation_size = 32
    conf.max_growth_while_training = 0.2

    conf.validation_split = 0.8
    conf.batch_size = 32
    conf.epochs = 4
    conf.max_sample_count = 100000
    conf.run_post_training_cmds = []

    return conf


class WorkerInfo(object):
    def __init__(self, worker, ping_time):
        self.worker = worker
        self.valid = True
        self.worker_type = None
        self.ping_time_sent = ping_time
        self.reset()

    def reset(self):
        if self.worker_type == "approx_self_play":
            self.configured = False

            # sent out up to this amount
            self.unique_state_index = 0

    def get_and_update(self, unique_states):
        assert self.worker_type == "approx_self_play"
        assert self.configured
        new_states = unique_states[self.unique_state_index:]
        self.unique_state_index += len(new_states)
        return new_states

    def cleanup(self):
        self.valid = False


class ServerBroker(Broker):
    def __init__(self, conf_filename):
        Broker.__init__(self)

        self.conf_filename = conf_filename
        if os.path.exists(conf_filename):
            conf = attrutil.json_to_attr(open(conf_filename).read())
            assert isinstance(conf, msgdefs.ServerConfig)
        else:
            conf = default_conf()

        attrutil.pprint(conf)

        self.conf = conf

        self.game_info = lookup.by_name(self.conf.game)

        self.workers = {}
        self.free_players = []
        self.the_nn_trainer = None

        self.accumulated_samples = []
        self.unique_states_set = set()
        self.unique_states = []

        # when a generation object is around, we are in the processing of training
        self.generation = None
        self.cmds_running = None

        self.register(msgdefs.Pong, self.on_pong)

        self.register(msgdefs.Ok, self.on_ok)
        self.register(msgdefs.RequestSampleResponse, self.on_sample_response)

        self.networks_reqd_trained = 0

        self.check_nn_generations_exist()
        self.create_approx_config()
        self.save_our_config()

        # finally start listening on port
        reactor.listenTCP(conf.port, ServerFactory(self))

    def check_nn_generations_exist(self):
        score_gen = self.get_score_generation(self.conf.current_step)
        policy_gen = self.get_policy_generation(self.conf.current_step)

        log.debug("current policy gen %s" % score_gen)
        log.debug("current score gen %s" % policy_gen)

        for g in (policy_gen, score_gen):
            net = network.create(g, self.game_info, load=False)
            if not net.can_load():
                # will create a randon network
                if self.conf.current_step == 0:
                    net.save()
                else:
                    critical_error("Did not find network %s.  exiting." % g)

    def save_our_config(self, rolled=False):
        if os.path.exists(self.conf_filename):
            if rolled:
                shutil.copy(self.conf_filename, self.conf_filename + "-%00d" % (self.conf.current_step - 1))
            else:
                shutil.copy(self.conf_filename, self.conf_filename + "-bak")

        with open(self.conf_filename, 'w') as open_file:
            open_file.write(attrutil.attr_to_json(self.conf, indent=4))

    def get_master_by_ip(self):
        ''' spin through self play workers, and gets the first worker for a new ip.  returns list '''
        pass

    def get_policy_generation(self, step):
        return "%sgen_%s_%s" % (self.conf.generation_prefix,
                                self.conf.policy_network_size,
                                step)

    def get_score_generation(self, step):
        return "%sgen_%s_%s" % (self.conf.generation_prefix,
                                self.conf.score_network_size,
                                step)

    def need_more_samples(self):
        return len(self.accumulated_samples) < (self.conf.generation_size +
                                                self.conf.generation_size * self.conf.max_growth_while_training)

    def new_worker(self, worker):
        self.workers[worker] = WorkerInfo(worker, time.time())
        log.debug("New worker %s" % worker)
        worker.send_msg(msgdefs.Ping())

    def remove_worker(self, worker):
        if worker not in self.workers:
            log.critical("worker removed, but not in workers %s" % worker)
        self.workers[worker].cleanup()
        del self.workers[worker]
        if worker == self.the_nn_trainer:
            self.the_nn_trainer = None

    def on_pong(self, worker, msg):
        info = self.workers[worker]
        log.info("worker %s, ping/pong time %.3f msecs" % (worker,
                                                           (time.time() - info.ping_time_sent) * 1000))

        if info.worker_type is None:
            self.init_worker(info, msg.worker_type)

    def init_worker(self, info, worker_type):
        info.worker_type = worker_type

        if info.worker_type == "approx_self_play":
            info.reset()

            self.free_players.append(info)

            # configure player will happen in schedule_players
            reactor.callLater(0, self.schedule_players)

        elif info.worker_type == "nn_train":
            # protection against > 1 the_nn_trainer
            if self.the_nn_trainer is not None:
                raise Exception("the_nn_trainer already set")

            self.the_nn_trainer = info

        else:
            log.error("Who are you? %s" % (info.worker_type))
            raise Exception("Who are you?")

    def on_ok(self, worker, msg):
        info = self.workers[worker]
        if msg.message == "configured":
            info.configured = True
            self.free_players.append(info)
            reactor.callLater(0, self.schedule_players)

        if msg.message == "network_trained":
            self.networks_reqd_trained -= 1
            if self.networks_reqd_trained == 0:
                if self.conf.run_post_training_cmds:
                    self.cmds_running = runprocs.RunCmds(self.conf.run_post_training_cmds,
                                                         cb_on_completion=self.finished_cmds_running,
                                                         max_time=10.0)
                    self.cmds_running.spawn()
                else:
                    self.roll_generation()

    def finished_cmds_running(self):
        self.cmds_running = None
        log.info("commands done")
        self.roll_generation()

    def on_sample_response(self, worker, msg):
        info = self.workers[worker]
        state = tuple(msg.sample.state)

        # need to check it isn't a duplicate and drop it
        if state in self.unique_states_set:
            log.warning("dropping inflight duplicate state")

        else:
            self.unique_states_set.add(state)
            self.unique_states.append(state)
            self.accumulated_samples.append(msg.sample)

            assert len(self.unique_states_set) == len(self.accumulated_samples)

        log.info("len accumulated_samples: %s" % len(self.accumulated_samples))
        log.info("worker saw %s duplicates" % msg.duplicates_seen)

        self.free_players.append(info)
        reactor.callLater(0, self.schedule_players)

    def new_generation(self):
        assert len(self.accumulated_samples) > self.conf.generation_size

        if self.generation is not None:
            return

        log.info("new_generation()")

        gen = msgdefs.Generation()
        gen.game = self.conf.game
        gen.with_score_generation = self.get_score_generation(self.conf.current_step)
        gen.with_policy_generation = self.get_policy_generation(self.conf.current_step)
        gen.num_samples = self.conf.generation_size
        gen.samples = self.accumulated_samples[:self.conf.generation_size]

        # write json file
        json.encoder.FLOAT_REPR = lambda f: ("%.5f" % f)

        log.info("writing json")
        filename = os.path.join(self.conf.store_path, "gendata_%s_%s.json" % (self.conf.game,
                                                                              self.conf.current_step))
        with open(filename, 'w') as open_file:
            open_file.write(attrutil.attr_to_json(gen, indent=4))

        self.generation = gen
        if self.the_nn_trainer is None:
            critical_error("There is no nn trainer to create network - exiting")

        log.info("create TrainNNRequest()")
        m = msgdefs.TrainNNRequest()
        m.game = self.conf.game
        m.generation_prefix = self.conf.generation_prefix
        m.store_path = self.conf.store_path

        m.use_previous = False  # until we are big enough, what is the point?

        m.next_step = self.conf.current_step + 1
        m.validation_split = self.conf.validation_split
        m.batch_size = self.conf.batch_size
        m.epochs = self.conf.epochs
        m.max_sample_count = self.conf.max_sample_count

        # send out message to train
        if self.conf.policy_network_size != self.conf.score_network_size:
            for network_size in (self.conf.policy_network_size, self.conf.score_network_size):
                m.network_size = network_size

                self.the_nn_trainer.worker.send_msg(m)
                log.info("sent to the_nn_trainer")
                self.networks_reqd_trained += 1
        else:
            m.network_size = self.conf.policy_network_size
            log.info("sent to the_nn_trainer")
            self.the_nn_trainer.worker.send_msg(m)
            self.networks_reqd_trained += 1

    def roll_generation(self):
        # training is done
        self.conf.current_step += 1
        self.check_nn_generations_exist()

        # reconfigure player workers
        for _, info in self.workers.items():
            info.reset()

        self.create_approx_config()

        # rotate these
        self.accumulated_samples = self.accumulated_samples[self.conf.generation_size:]
        self.unique_states = self.unique_states[self.conf.generation_size:]
        self.unique_states_set = set(self.unique_states)

        assert len(self.accumulated_samples) == len(self.unique_states)
        assert len(self.unique_states) == len(self.unique_states_set)

        # store the server config
        self.save_our_config(rolled=True)

        self.generation = None
        log.warning("roll_generation() complete.  We have %s samples leftover" % len(self.accumulated_samples))
        self.schedule_players()

    def create_approx_config(self):
        # we use score_gen for select also XXX we should probably just go to one
        policy_generation = self.get_policy_generation(self.conf.current_step)
        score_generation = self.get_score_generation(self.conf.current_step)

        self.conf.player_select_conf.generation = score_generation
        self.conf.player_policy_conf.generation = policy_generation
        self.conf.player_score_conf.generation = score_generation

        conf = msgdefs.ConfigureApproxTrainer(game=self.conf.game)
        conf.player_select_conf = self.conf.player_select_conf
        conf.player_policy_conf = self.conf.player_policy_conf
        conf.player_score_conf = self.conf.player_score_conf

        self.approx_play_config = conf

    def schedule_players(self):
        if not self.free_players:
            return

        new_free_players = []
        for worker_info in self.free_players:
            if not worker_info.valid:
                continue

            if not worker_info.configured:
                worker_info.worker.send_msg(self.approx_play_config)

            else:
                if self.need_more_samples():
                    updates = worker_info.get_and_update(self.unique_states)
                    m = msgdefs.RequestSample(updates)
                    log.debug("sending request with %s updates" % len(updates))
                    worker_info.worker.send_msg(m)
                else:
                    log.warning("capacity full! %d" % len(self.accumulated_samples))
                    new_free_players.append(worker_info)

        self.free_players = new_free_players

        if len(self.accumulated_samples) > self.conf.generation_size:
            self.new_generation()

        if self.the_nn_trainer is None:
            log.warning("There is no nn trainer - please start")


def start_server_factory():
    from ggplib.util.init import setup_once
    setup_once("worker")

    ServerBroker(sys.argv[1])

    reactor.run()


if __name__ == "__main__":
    start_server_factory()
