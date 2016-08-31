import logging
import threading
import os
import traceback
import time
import platform
import signal
from subprocess import Popen, PIPE, STDOUT
try:
    from Queue import Queue, Empty
except ImportError:
    from queue import Queue, Empty  # python 3.x

class TaskProcessor(object):

    def __init__(self, task, widget, worker, make_workdir=True, save_widget_code=True):
        self.widget = widget
        self.task = task
        task.processor = self
        self.worker = worker
        self.meteorClient = self.worker.meteorClient
        assert not self.task is None
        self.isSubTask = bool(self.task.get('parent'))
        self.id = self.task.id
        self.parentId = None if not task.get('parent') else task.get('parent')
        FORMAT = '%(asctime)-15s %(clientip)s %(user)-8s %(message)s'
        self.logger = logging.getLogger('taskProcessor')
        self.abort = self.task.abort
        self.exception = None
        self.running = False
        if self.task.get('parent') is None or self.task.get('parent') == '':
            self.workdir = os.path.abspath(os.path.join(
                self.worker.workdir, self.widget.id, self.id))
        else:
            self.workdir = os.path.abspath(os.path.join(
                self.worker.workdir, self.widget.id, self.task.get('parent'), self.id))
        if make_workdir:
            self.make_workdir()
        self.updateCallbackDict = {}
        self.removeCallbackList = []
        self.requirements = {}
        self.resourcesOcuppied = {}
        if save_widget_code:
            self.save_widget_code()
        self.init()

    def save_widget_code(self, name):
        if self.widget.get('code_snippets'):
            self.make_workdir()
            for k in self.widget.get('code_snippets'):
                code = self.widget.get('code_snippets')[k]
                with open(os.path.join(self.workdir, code['name']), 'w') as f:
                    f.write(code['content'])

    def init(self):
        pass

    def on_update(self, field, callback):
        if self.updateCallbackDict.has_key(field):
            self.updateCallbackDict[field].append(callback)
        else:
            self.updateCallbackDict[field] = [callback]

    def get_widget_code(self, name):
        name = name.replace('.', '_')
        if self.widget.get('code_snippets') and self.widget.get('code_snippets').has_key(name):
            return self.widget.get('code_snippets')[name]['content']
        else:
            return None

    def on_remove(self, callback):
        self.removeCallbackList.append(callback)

    def make_workdir(self):
        if not os.path.exists(self.workdir):
            os.makedirs(self.workdir)

    def update(self, key, value):
        try:
            if value is None and type(key) == type(dict()):
                self.meteorClient.call('tasks.update.worker', [self.id, self.worker.id, self.worker.token, {'$set': key}])
            else:
                self.meteorClient.call('tasks.update.worker', [self.id, self.worker.id, self.worker.token, {'$set': {key: value}}])
        except Exception as e:
            print('error ocurred during setting ' + key)

    def stop(self):
        self.abort.set()
        for subtask in self.task.subtasks:
            if subtask.processor:
                subtask.processor.abort.set()
        self.task.set('status.stage', 'aborting')
        print('stopping task...')

    def start(self, resources=None):
        self.abort.clear()
        self.running = True
        self.task.set('status.stage', 'starting')
        self.task.set('status.running', True)
        self.task.set('status.progress', -1)
        self.task.set('status.error', '')
        print('starting...')
        try:
            self.before()
            self.run(resources)
        except Exception as e:
            print('error from task, taskName:{} taskId:{} widgetId:{}'.format(self.task.get('name'), self.task.id, self.task.get('widgetId')))
            traceback.print_exc()
            self.task.set('status.error', traceback.format_exc())
            self.end(force_quit=True)

    def end(self, force_quit=False):
        try:
            if not force_quit and len(self.task.subtasks) > 0:
                self.task.set('status.stage', 'finishing')
                lastfinished = 0
                while True:
                    finished = 0
                    total = len(self.task.subtasks)
                    for t in self.task.subtasks:
                        if not self.task.processor.running:
                            finished += 1
                        if self.abort.is_set():
                            break
                    if finished == total:
                        break
                    if self.abort.is_set():
                        break
                    if lastfinished != finished:
                        self.task.set('status.stage', '{}/{}'.format(finished,total))
                        lastfinished = finished
                    time.sleep(0.5)

                if self.abort.is_set():
                    self.task.set('status.stage', 'abort')
                else:
                    self.task.set('status.stage', 'done')
                    self.task.set('status.progress', 100)

            self.task.set('cmd', '')
            if self.task.get('status.progress') < 0:
                self.task.set('status.progress', 0)
            if 'ing' in self.task.get('status.stage'):
                self.task.set('status.stage', 'exited')

        except Exception as e:
            print('error from task, taskName:{} taskId:{} widgetId:{}'.format(self.task.get('name'), self.task.id, self.task.get('widgetId')))
            traceback.print_exc()
            self.task.set('status.error', traceback.format_exc())
        finally:
            self.running = False
            self.task.set('status.running', False)
            self.task.set('visible2worker', False)
            try:
                self.after()
            except Exception as e:
                print('error from task, taskName:{} taskId:{} widgetId:{}'.format(self.task.get('name'), self.task.id, self.task.get('widgetId')))
                traceback.print_exc()

    def before(self):
        pass
        #self.task.set('output', {})

    def after(self):
        pass

    def after_runtime_error(self):
        print('after_runtime_error')

    def task_arguments(self, resources, env):
        return []

    def name(self):
        return self.task.get('name')

    def run(self, resources=None):
        '''
        should call self.end() after all process is over
        '''
        self.task.set('status.info', 'run is not implemented')
        self.end()

    def check_subtasks(self):
        status = {}
        prog = 0
        finished = 0
        for subtask in self.task.subtasks:
            prog +=subtask.get('status.progress')
            if not subtask.processor.running:
                finished +=1
        status['progress'] = prog
        status['finished'] = finished
        status['total'] = len(self.task.subtasks)
        return status


class ThreadedTaskProcessor(TaskProcessor):
    def __init__(self, task, widget, worker, process=None, **kwargs):
        self.process = process
        super(ThreadedTaskProcessor, self).__init__(task, widget, worker, **kwargs)

    def task_arguments(self, resources, env):
        return []

    def run(self, resources=None):
        self.taskThread = threading.Thread(target=self.run_thread,
                                           args=self.task_arguments(resources, None))
        self.taskThread.daemon = True
        self.taskThread.start()

    def run_thread(self, *args):
        try:
            self.process_task(*args)
            if self.process:
                self.process(self.task, *args)
        except Exception as e:
            print('error from task, taskName:{} taskId:{} widgetId:{}'.format(self.task.get('name'), self.task.id, self.task.get('widgetId')))
            traceback.print_exc()
            self.task.set('status.error', traceback.format_exc())
        finally:
            self.end()

    def process_task(self, *args):
        pass


class ProcessTaskProcessor_(TaskProcessor):

    def process_output(self, line):
        self.task.set('status.progress', int(line))
        print('processing line: ' + line)
        sys.stdout.flush()
        return True

    def task_arguments(self, resources, env):
        return ['python', 'dummytask.py']

    def periodic_check(self, process):
        pass

    def run(self, resources=None):
        env = os.environ.copy()
        args = self.task_arguments(resources, env)
        if not args:
            print('error from task, taskName:{} taskId:{} widgetId:{}'.format(self.task.get('name'), self.task.id, self.task.get('widgetId')))
            self.logger.error('Could not create the arguments for Popen')
            self.task.set('status.error', 'Could not create the arguments for Popen')
            return False
        # Convert them all to strings
        args = [str(x) for x in args if str(x) != '']
        self.logger.info('%s task started.' % self.name())
        unrecognized_output = []
        import sys
        env['PYTHONPATH'] = os.pathsep.join(
            ['.', self.workdir, env.get('PYTHONPATH', '')] + sys.path)
        # https://docs.python.org/2/library/subprocess.html#converting-argument-sequence
        if platform.system() == 'Windows':
            args = ' '.join(args)
        self.logger.info('Task subprocess args: {}'.format(args))
        try:
            p = Popen(args, bufsize=0, stdout=PIPE, stderr=STDOUT,
                      shell=False, universal_newlines=True)
        except Exception as e:
            print('error from task, taskName:{} taskId:{} widgetId:{}'.format(self.task.get('name'), self.task.id, self.task.get('widgetId')))
            traceback.print_exc()
            self.task.set('status.error', traceback.format_exc())
            self.end(force_quit=True)
            return False
        # run the shell as a subprocess:

        nbsr = NonBlockingStreamReader(p.stdout)
        try:
            sigterm_time = None  # When was the SIGTERM signal sent
            sigterm_timeout = 2  # When should the SIGKILL signal be sent
            # get the output
            endofstream = False
            while p.poll() is None or not endofstream:
                try:
                    line = nbsr.readline(0.1)
                except(UnexpectedEndOfStream):
                    endofstream = True
                try:
                    self.periodic_check(p)
                except:
                    traceback.print_exc()

                if line is not None:
                    # Remove whitespace
                    line = line.strip()
                if line:
                    try:
                        if not self.process_output(line):
                            self.logger.warning('%s unrecognized output: %s' % (
                                self.name(), line.strip()))
                            unrecognized_output.append(line)
                    except:
                        print('error from task, taskName:{} taskId:{} widgetId:{}'.format(self.task.get('name'), self.task.id, self.task.get('widgetId')))
                        traceback.print_exc()
                        self.task.set('status.error', traceback.format_exc())
                else:
                    time.sleep(0.05)

                if self.abort.is_set():
                    if sigterm_time is None:
                        # Attempt graceful shutdown
                        p.send_signal(signal.SIGTERM)
                        sigterm_time = time.time()
                if sigterm_time is not None and (time.time() - sigterm_time > sigterm_timeout):
                    p.send_signal(signal.SIGKILL)
                    self.logger.warning(
                        'Sent SIGKILL to task "%s"' % self.name())
                    time.sleep(0.1)
        except:
            traceback.print_exc()
            try:
                p.terminate()
            except Exception as e:
                print('error occured during terminating a process.')
            raise
        if self.abort.is_set():
            self.end(force_quit=True)
            return False
        elif p.returncode != 0:
            # Report that this task is finished
            self.logger.error('%s task failed with error code %s' %
                              (self.name(), str(p.returncode)))
            if self.exception is None:
                self.exception = 'error code %s' % str(p.returncode)
                if unrecognized_output:
                    if self.traceback is None:
                        self.traceback = '\n'.join(unrecognized_output)
                    else:
                        self.traceback = self.traceback + \
                            ('\n'.join(unrecognized_output))
            self.after_runtime_error()
            print('error from task, taskName:{} taskId:{} widgetId:{}'.format(self.task.get('name'), self.task.id, self.task.get('widgetId')))
            self.task.set('status.error', '%s task failed with error code %s' % (self.name(), str(p.returncode)))
            self.end(force_quit=True)
            return False
        else:
            self.task.set('status.progress', 100)
            self.end(force_quit=True)
            self.logger.info('%s task completed.' % self.name())
            return True

class ProcessTaskProcessor(ProcessTaskProcessor_):
    def run(self, resources=None):
        self.taskThread = threading.Thread(target=self.run_thread,
                                           args=[resources])
        self.taskThread.daemon = True
        self.taskThread.start()

    def run_thread(self, *args):
        try:
            super(ProcessTaskProcessor, self).run(*args)
        except Exception as e:
            print('error from task, taskName:{} taskId:{} widgetId:{}'.format(self.task.get('name'), self.task.id, self.task.get('widgetId')))
            traceback.print_exc()
            self.task.set('status.error', traceback.format_exc())
        finally:
            self.end()

class NonBlockingStreamReader:

    def __init__(self, stream):
        '''
        stream: the stream to read from.
                Usually a process' stdout or stderr.
        '''
        self._s = stream
        self._q = Queue()

        def _populateQueue(stream, queue):
            '''
            Collect lines from 'stream' and put them in 'quque'.
            '''

            while True:
                line = stream.readline()
                if line:
                    queue.put(line)
                else:
                    self.end = True
                    break
                    #raise UnexpectedEndOfStream
                time.sleep(0.01)
        self.end = False
        self._t = threading.Thread(target=_populateQueue,
                                   args=(self._s, self._q))
        self._t.daemon = True
        self._t.start()  # start collecting lines from the stream

    def readline(self, timeout=None):
        try:
            return self._q.get(block=timeout is not None,
                               timeout=timeout)
        except Empty:
            if self.end:
                raise UnexpectedEndOfStream
            return None


class UnexpectedEndOfStream(Exception):
    pass
