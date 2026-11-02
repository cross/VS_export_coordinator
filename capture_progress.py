"""Elapsed-time progress for local audits, including long CPU-bound checks."""
import shutil
import sys
import threading
import time


class AuditProgress:
    """The worker only renders progress; all audit work stays on the caller."""
    def __init__(self,stream=None,interval=5,clock=None):
        self.stream=stream if stream is not None else sys.stdout
        self.interval=interval
        self.clock=clock or time.monotonic
        self.terminal=self.stream.isatty()
        self._lock=threading.Lock()
        self._stop=threading.Event()
        self._thread=None
        self._label=None
        self._width=0

    def __enter__(self):
        self._thread=threading.Thread(target=self._watch,name='capture-audit-progress',daemon=True)
        self._thread.start()
        return self

    def __exit__(self,kind,value,traceback):
        self._stop.set()
        self._thread.join()
        if self._label is not None:
            self.finish('interrupted' if kind is KeyboardInterrupt else 'stopped')

    def _write(self,message,finished=False):
        if self.terminal:
            if not finished:
                width=max(20,shutil.get_terminal_size(fallback=(100,24)).columns-1)
                if len(message)>width:
                    tail=min(24,width//2)
                    message=message[:width-tail-3]+'...'+message[-tail:]
            self.stream.write(('\r' if self._width else '')+message+
                              ' '*max(0,self._width-len(message))+('\n' if finished else ''))
            self._width=0 if finished else len(message)
        else:
            self.stream.write(message.rstrip()+'\n')
        self.stream.flush()

    def begin(self,label,detail=''):
        with self._lock:
            if self._label is not None:
                raise RuntimeError('Finish the current audit progress step first')
            self._label=label
            self._initial='  '+label+(': '+detail if detail else '')+' ... '
            self._detail=detail
            self._started=self._last_notice=self.clock()
            self._write(self._initial)

    def update(self,detail):
        with self._lock:
            if self._label is not None:
                self._detail=detail

    def refresh(self):
        with self._lock:
            if self._label is None:
                return
            now=self.clock()
            if now-self._last_notice<self.interval:
                return
            self._last_notice=now
            message='  '+self._label+(': '+self._detail if self._detail else '')
            self._write(f'{message} ({now-self._started:.0f}s elapsed).')

    def finish(self,result):
        with self._lock:
            if self._label is None:
                return
            elapsed=self.clock()-self._started
            self._write(f'{self._initial}{result} ({elapsed:.1f}s).',finished=True)
            self._label=None

    def _watch(self):
        while not self._stop.wait(self.interval):
            try:
                self.refresh()
            except OSError:
                return  # A closed diagnostic stream must not interrupt auditing.
