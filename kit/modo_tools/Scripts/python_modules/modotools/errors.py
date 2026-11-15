# python
"""OpError: how an op fails on purpose, with a type the agent can act on.

run_job turns an OpError into an answer with that type, its message, a hint
and the Modo event log lines. Anything else an op raises is a job_exception:
a bug in the kit, not in what the agent asked for.
"""


class OpError(Exception):
    def __init__(self, err_type, message, hint="", traceback_text="",
                 event_log=None):
        Exception.__init__(self, message)
        self.type = err_type
        self.message = message
        self.hint = hint
        self.traceback = traceback_text
        self.event_log = event_log or []
