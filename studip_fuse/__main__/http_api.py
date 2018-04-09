from flask import Flask, jsonify

from studip_fuse.__main__.fs_driver import FUSEView

app = Flask("studip_fuse.http")
fuseview_inst = None  # type: FUSEView


def run(fuseview):
    global fuseview_inst
    fuseview_inst = fuseview
    app.run()


def await_threadsafe(coro):
    import asyncio
    return asyncio.run_coroutine_threadsafe(coro, fuseview_inst.loop).result()


@app.route("/cache_stats")
def cache_stats():
    from studip_fuse.cache import AsyncTaskCache
    return AsyncTaskCache.format_all_statistics()


@app.route("/model_stats")
def model_stats():
    return jsonify(fuseview_inst.session.model_cache_stats())


@app.route("/clear_caches", methods=["POST"])
def clear_caches():
    from studip_fuse.cache import AsyncTaskCache
    return await_threadsafe(AsyncTaskCache.clear_all_caches())


@app.route("/save_model", methods=["POST"])
def save_model():
    return await_threadsafe(fuseview_inst.session.save_model())


@app.route("/load_model", methods=["POST"])
def load_model():
    return await_threadsafe(fuseview_inst.session.load_model())