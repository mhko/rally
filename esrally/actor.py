import faulthandler
import logging
import signal
import sys
import time
import os

import thespian.actors
import thespian.system.messages.status
from esrally import exceptions
from esrally.utils import console, io, net

logger = logging.getLogger("rally.actor")

root_log_level = logging.INFO
es_log_level = logging.WARNING


class BenchmarkFailure:
    """
    Indicates a failure in the benchmark execution due to an exception
    """
    def __init__(self, message, cause=None):
        self.message = message
        self.cause = cause


class BenchmarkCancelled:
    """
    Indicates that the benchmark has been cancelled (by the user).
    """
    pass


class RallyActor(thespian.actors.Actor):
    def __init__(self):
        super().__init__()
        # allow to see a thread-dump on SIGQUIT
        faulthandler.register(signal.SIGQUIT, file=sys.stderr)
        self.children = []
        self.received_responses = []
        self.status = None

    @staticmethod
    def configure_logging(actor_logger):
        # configure each actor's root logger
        actor_logger.parent.setLevel(root_log_level)
        # Also ensure that the elasticsearch logger is properly configured
        logging.getLogger("elasticsearch").setLevel(es_log_level)

    # The method name is required by the actor framework
    # noinspection PyPep8Naming
    @staticmethod
    def actorSystemCapabilityCheck(capabilities, requirements):
        for name, value in requirements.items():
            current = capabilities.get(name, None)
            if current != value:
                # A mismatch by is not a problem by itself as long as at least one actor system instance matches the requirements.
                logger.info("Checking capabilities [%s] against requirements [%s] failed." % (capabilities, requirements))
                return False
        logger.info("Capabilities [%s] match requirements [%s]." % (capabilities, requirements))
        return True

    def transition_when_all_children_responded(self, sender, msg, expected_status, new_status, transition):
        """

        Waits until all children have sent a specific response message and then transitions this actor to a new status.

        :param sender: The child actor that has responded.
        :param msg: The response message.
        :param expected_status: The status in which this actor should be upon calling this method.
        :param new_status: The new status once all child actors have responded.
        :param transition: A parameter-less function to call immediately after changing the status.
        """
        if self.is_current_status_expected(expected_status):
            self.received_responses.append(msg)
            response_count = len(self.received_responses)
            expected_count = len(self.children)

            logger.info("[%d] of [%d] child actors have responded for transition from [%s] to [%s]." %
                        (response_count, expected_count, self.status, new_status))
            if response_count == expected_count:
                logger.info("All [%d] child actors have responded. Transitioning now from [%s] to [%s]." %
                            (expected_count, self.status, new_status))
                # all nodes have responded, change status
                self.status = new_status
                self.received_responses = []
                transition()
            elif response_count > expected_count:
                raise exceptions.RallyAssertionError(
                    "Received [%d] responses but only [%d] were expected to transition from [%s] to [%s]. The responses are: %s" %
                    (response_count, expected_count, self.status, new_status, self.received_responses))
        else:
            raise exceptions.RallyAssertionError("Received [%s] from [%s] but we are in status [%s] instead of [%s]." %
                                                 (type(msg), sender, self.status, expected_status))

    def send_to_children_and_transition(self, sender, msg, expected_status, new_status):
        """

        Sends the provided message to all child actors and immediately transitions to the new status.

        :param sender: The actor from which we forward this message (in case it is message forwarding). Otherwise our own address.
        :param msg: The message to send.
        :param expected_status: The status in which this actor should be upon calling this method.
        :param new_status: The new status.
        """
        if self.is_current_status_expected(expected_status):
            logger.info("Transitioning from [%s] to [%s]." % (self.status, new_status))
            self.status = new_status
            for m in self.children:
                self.send(m, msg)
        else:
            raise exceptions.RallyAssertionError("Received [%s] from [%s] but we are in status [%s] instead of [%s]." %
                                                 (type(msg), sender, self.status, expected_status))

    def is_current_status_expected(self, expected_status):
        # if we don't expect anything, we're always in the right status
        if not expected_status:
            return True
        # do an explicit check for a list here because strings are also iterable and we have very tight control over this code anyway.
        elif isinstance(expected_status, list):
            return self.status in expected_status
        else:
            return self.status == expected_status


# Defined on top-level to allow pickling
class ActorLogFilter(logging.Filter):
    def filter(self, log_record):
        return "actorAddress" in log_record.__dict__


# Defined on top-level to allow pickling
class NotActorLogFilter(logging.Filter):
    def filter(self, log_record):
        return "actorAddress" not in log_record.__dict__


# Defined on top-level to allow pickling
def configure_utc_formatter(*args, **kwargs):
    formatter = logging.Formatter(fmt=kwargs["fmt"], datefmt=kwargs["datefmt"])
    formatter.converter = time.gmtime
    return formatter


def configure_actor_logging():
    log_dir = "%s/.rally/logs" % os.path.expanduser("~")
    io.ensure_dir(log_dir)

    # actor_log_handler = {"class": "logging.handlers.SysLogHandler", "address": "/var/run/syslog"}
    # actor_messages_handler = {"class": "logging.handlers.SysLogHandler", "address": "/var/run/syslog"}

    return {
        "version": 1,
        "formatters": {
            "normal": {
                "fmt": "%(asctime)s,%(msecs)d PID:%(process)d %(name)s %(levelname)s %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
                "()": configure_utc_formatter
            },
            "actor": {
                "fmt": "%(asctime)s,%(msecs)d PID:%(process)d %(name)s %(levelname)s %(actorAddress)s => %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
                "()": configure_utc_formatter
            }
        },
        "filters": {
            "isActorLog": {
                "()": ActorLogFilter
            },
            "notActorLog": {
                "()": NotActorLogFilter
            }
        },
        "handlers": {
            "rally_log_handler": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "filename": "%s/rally-actors.log" % log_dir,
                "when": "midnight",
                "backupCount": 14,
                "encoding": "UTF-8",
                "formatter": "normal",
                "filters": ["notActorLog"],
                "level": root_log_level
            },
            "actor_log_handler": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "filename": "%s/rally-actor-messages.log" % log_dir,
                "when": "midnight",
                "backupCount": 14,
                "encoding": "UTF-8",
                "formatter": "actor",
                "filters": ["isActorLog"],
                "level": root_log_level
            }
        },
        "root": {
            "handlers": ["rally_log_handler", "actor_log_handler"],
            "level": root_log_level
        },
        "loggers": {
            "elasticsearch": {
                "handlers": ["rally_log_handler"],
                "level": es_log_level,
                # don't let the root logger handle it again
                "propagate": 0
            }
        }
    }


def actor_system_already_running(ip="127.0.0.1"):
    """
    Determines whether an actor system is already running by opening a socket connection.

    Note: It may be possible that another system is running on the same port.
    """
    import socket
    s = socket.socket()
    try:
        s.connect((ip, 1900))
        s.close()
        return True
    except Exception:
        return False


__SYSTEM_BASE = "multiprocTCPBase"


def use_offline_actor_system():
    global __SYSTEM_BASE
    __SYSTEM_BASE = "multiprocQueueBase"


def bootstrap_actor_system(try_join=False, prefer_local_only=False, local_ip=None, coordinator_ip=None):
    system_base = __SYSTEM_BASE
    try:
        if try_join:
            if actor_system_already_running():
                logger.info("Joining already running actor system with system base [%s]." % system_base)
                return thespian.actors.ActorSystem(system_base)
            else:
                logger.info("Creating new actor system with system base [%s] on coordinator node." % system_base)
                # if we try to join we can only run on the coordinator...
                return thespian.actors.ActorSystem(system_base, logDefs=configure_actor_logging(), capabilities={"coordinator": True})
        elif prefer_local_only:
            coordinator = True
            if system_base != "multiprocQueueBase":
                coordinator_ip = "127.0.0.1"
                local_ip = "127.0.0.1"
            else:
                coordinator_ip = None
                local_ip = None
        else:
            if system_base != "multiprocTCPBase" and system_base != "multiprocUDPBase":
                raise exceptions.SystemSetupError("Rally requires a network-capable system base but got [%s]." % system_base)
            if not coordinator_ip:
                raise exceptions.SystemSetupError("coordinator IP is required")
            if not local_ip:
                raise exceptions.SystemSetupError("local IP is required")
            # always resolve the public IP here, even if a DNS name is given. Otherwise Thespian will be unhappy
            local_ip = net.resolve(local_ip)
            coordinator_ip = net.resolve(coordinator_ip)

            coordinator = local_ip == coordinator_ip

        capabilities = {"coordinator": coordinator}
        if local_ip:
            # just needed to determine whether to run benchmarks locally
            capabilities["ip"] = local_ip
        if coordinator_ip:
            # Make the coordinator node the convention leader
            capabilities["Convention Address.IPv4"] = "%s:1900" % coordinator_ip
        logger.info("Starting actor system with system base [%s] and capabilities [%s]." % (system_base, capabilities))
        return thespian.actors.ActorSystem(system_base,
                                           logDefs=configure_actor_logging(),
                                           capabilities=capabilities)
    except thespian.actors.ActorSystemException:
        logger.exception("Could not initialize internal actor system. Terminating.")
        console.error("Could not initialize successfully.\n")
        console.error("Are there are still processes from a previous race?")
        console.error("Please check and terminate related Python processes before running Rally again.\n")
        raise
