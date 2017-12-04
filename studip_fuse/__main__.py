import argparse as argparse
import asyncio
import concurrent.futures
import functools
import logging
import os
import sys
import threading
import traceback
import warnings
from getpass import getpass
from threading import Thread

import appdirs
import sh
from fuse import FUSE
from more_itertools import flatten, one

from studip_fuse.cached_session import CachedStudIPSession
from studip_fuse.fs_driver import FUSEView, LoggingFUSEView
from studip_fuse.real_path import RealPath
from studip_fuse.virtual_path import VirtualPath

logging.basicConfig(level=logging.INFO)
thread_log = logging.getLogger("threads")


def main():
    args = parse_args()
    if args.debug:
        logging.root.setLevel(logging.DEBUG)
        warnings.resetwarnings()
    else:
        logging.getLogger("sh").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)

    future = concurrent.futures.Future()
    loop_thread = Thread(target=functools.partial(run_loop, args, future), name="aio event loop", daemon=True)
    loop_thread.start()
    loop, session = None, None
    try:
        logging.debug("Loop thread started, waiting for session initialization")
        loop, session = future.result()

        run_fuse(loop, session, args)
    except:
        logging.error("FUSE driver crashed", exc_info=True)
    finally:
        logging.info("FUSE driver stopped, also stopping event loop")
        future.cancel()
        if loop:
            loop.stop()

        # print loop stack trace until the loop thread completed
        counter = 0
        while loop_thread.is_alive() and counter < 6:
            loop_thread.join(5)
            counter += 1
            if loop_thread.is_alive():
                if loop:
                    dump_loop_stack(loop)
                else:
                    thread_log.info("Waiting for loop thread to abort initialization...")
                    thread_log.debug("Thread stack trace:\n %s", format_stack(sys._current_frames()[loop_thread.ident]))

        if loop_thread.is_alive():
            logging.warning("Shutting down main thread and thus killing hung event loop daemon thread")


class StoreNameValuePair(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        values = flatten(v.split(',') for v in values)
        for value in values:
            if '=' in value:
                n, v = value.split('=')
                setattr(namespace, n, v)
            elif value.startswith("no"):
                setattr(namespace, value[2:], False)
            else:
                setattr(namespace, value, True)


def parse_args():
    from studip_fuse import __version__ as prog_version, __author__ as prog_author
    dirs = appdirs.AppDirs("Stud.IP-Fuse", prog_author)
    parser = argparse.ArgumentParser(description='Stud.IP Fuse', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('user', help='Stud.IP username')
    parser.add_argument('mount', help='path to mount point')
    parser.add_argument('--pwfile', help='path to password file or "-" to read from stdin',
                        default=os.path.join(dirs.user_config_dir, '.studip-pw'))
    parser.add_argument('--format', help='format specifier for virtual paths',
                        default="{semester-lexical-short}/{course}/{type}/{short-path}/{name}")
    parser.add_argument('--cache', help='path to cache directory', default=dirs.user_cache_dir)
    parser.add_argument('--studip', help='Stud.IP base URL', default="https://studip.uni-passau.de")
    parser.add_argument('--sso', help='SSO base URL', default="https://sso.uni-passau.de")
    parser.add_argument('--debug', help='enable debug mode', action='store_true')
    parser.add_argument('--allowroot', help='allow root to access the mounted directory',
                        action='store_true')
    parser.add_argument('--version', action='version', version="%(prog)s " + prog_version)
    parser.add_argument('-o', help='FUSE-like options', nargs='+', action=StoreNameValuePair)
    args = parser.parse_args()
    return args


def run_loop(args, future: concurrent.futures.Future):
    try:
        logging.info("Initializing asyncio event loop...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        if args.debug:
            loop.set_debug(True)
    except Exception as e:
        logging.debug("Loop initialization failed, propagating result back to main thread")
        future.set_exception(e)
        raise
    if future.cancelled():
        return

    try:
        try:
            logging.info("Opening StudIP session")
            if args.pwfile == "-":
                password = getpass()
            else:
                try:
                    with open(args.pwfile) as f:
                        password = f.read()
                except FileNotFoundError as e:
                    logging.warning("%s. Either specifiy a file from which your Stud.IP password can be read "
                                    "or use `--pwfile -` to enter it using a promt in the shell." % e)
                    future.set_exception(e)
                    return
            coro = CachedStudIPSession(
                user_name=args.user, password=password.strip(),
                studip_base=args.studip, sso_base=args.sso,
                cache_dir=args.cache
            ).__aenter__()
            password = ""
            task = asyncio.ensure_future(coro, loop=loop)
            future.add_done_callback(lambda f: task.cancel() if f.cancelled() else None)
            if future.cancelled():
                return
            session = loop.run_until_complete(task)
        except Exception as e:
            logging.debug("Session initialization failed, propagating result back to main thread")
            future.set_exception(e)
            raise
        if future.cancelled():
            return

        logging.debug("Loop and session ready, sending result back to main thread")
        future.set_result((loop, session))

        try:
            logging.info("Running asyncio event loop...")
            loop.run_forever()
        finally:
            logging.info("asyncio event loop stopped, cleaning up")
            try:
                loop.run_until_complete(shutdown_loop_async(loop, session))
                logging.info("Cleaned up, closing event loop")
            except:
                logging.warning("Clean-up failed, closing", exc_info=True)

    finally:
        if not future.done():
            logging.warning("Event loop thread did not report result back to main thread, will probably hang.")
        loop.close()
        logging.info("Event loop closed, shutdown complete")


async def shutdown_loop_async(loop, session):
    await session.__aexit__(*sys.exc_info())
    logging.debug("Session closed")
    await asyncio.sleep(1)
    await loop.shutdown_asyncgens()
    logging.debug("Loop drained")


def run_fuse(loop, session, args):
    logging.info("Initializing virtual file system")
    vp = VirtualPath(session=session, path_segments=[], known_data={}, parent=None,
                     next_path_segments=args.format.split("/"))
    rp = RealPath(parent=None, generating_vps={vp})

    os.makedirs(args.cache, exist_ok=True)
    try:
        os.makedirs(args.mount, exist_ok=True)
    except FileExistsError:  # if mountpoint was not unmounted properly
        pass
    try:
        sh.fusermount("-u", args.mount)
    except sh.ErrorReturnCode as e:
        if "entry for" not in str(e) or "not found in" not in str(e):
            logging.warning("Could not unmount mount path %s", args.mount, exc_info=True)
        else:
            logging.debug(e.stderr.decode("UTF-8", "replace").strip().split("\n")[-1])

    logging.debug("Initialization done, handing over to FUSE driver")
    if args.debug:
        fuse_ops = LoggingFUSEView(rp, loop)
    else:
        fuse_ops = FUSEView(rp, loop)
    logging.info("Ready")
    FUSE(fuse_ops, args.mount, foreground=True, allow_root=args.allowroot, debug=args.debug)


def dump_loop_stack(loop):
    current_task = asyncio.Task.current_task(loop=loop)
    pending_tasks = [t for t in asyncio.Task.all_tasks(loop=loop) if not t.done() and t is not current_task]
    loop_thread = one(t for t in threading.enumerate() if t.ident == loop._thread_id)
    loop_thread_stack = sys._current_frames()[loop_thread.ident]
    loop_thread_stack_trace = format_stack(loop_thread_stack)

    if thread_log.isEnabledFor(logging.DEBUG):
        thread_log.debug("Current task %s in loop %s in loop thread %s", current_task, loop, loop_thread)
        if current_task:
            thread_log.debug("Task stack trace:\n %s", format_stack(current_task.get_stack()))
        thread_log.debug("Thread stack trace:\n %s", loop_thread_stack_trace)
        pending_tasks_str = "\n".join(
            str(t) + "\n" + format_stack(t.get_stack())
            for t in pending_tasks)
        thread_log.debug("%s further pending tasks:\n %s", len(pending_tasks), pending_tasks_str)
    else:
        thread_log.info("Waiting for event loop to stop... (%s further pending tasks after current task %s "
                        "in loop %s in loop thread %s)", len(pending_tasks), current_task, loop, loop_thread)

    if len(pending_tasks) == 0 and current_task is None \
            and "epoll.poll(timeout, max_ev)" in loop_thread_stack_trace.strip().split("\n")[-1]:
        thread_log.warning("Event loop hangs in epoll selector without any tasks pending. "
                           "This is probably a python bug (https://bugs.python.org/issue29780).")


def format_stack(stack):
    return "".join(traceback.format_stack(stack[0] if isinstance(stack, list) else stack))


if __name__ == "__main__":
    main()
