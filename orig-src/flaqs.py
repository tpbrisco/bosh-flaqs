#!/usr/bin/python
#
# flask queues -- using queues to pull real data, and flash to present
# that data to the screen.
#
# Make sure to include
#   export PYTHONPATH=/home/brisco/python/lib:/home/brisco/python/lib/python2.7:/home/brisco/python/lib/python2.7/site-packages
# 

# basic OS setup
import sys, os
import time
import datetime
import sched

# for our presentation layer
from flask import Flask, request, url_for, render_template, redirect

# for "child" calls - we can call out to other services for this
import requests
import simplejson as json

# manage threads and thread requests
import threading
import Queue

# for stock lookups
import ystockquote as ysq

# see "setupThreads" for thread starts

# prototype commands available - copy and fill in values to use them
qStatusQ = {'statusQ':None,'Symbol':None}
qStatusR = {'statusR':None,'Symbol':None}
qTerminateQ = {'termQ':None,'Symbol':None}
qTerminateR = {'termR':None,'Symbol':None}
queryWeather = {'weather':None,'Symbol':None}  # just for fun

##
## Yahoo Finance "CSV" query routines.
## The best reference I can find for the "&f=" string is at (oddly)
## http://code.google.com/p/yahoo-finance-managed/wiki/enumQuoteProperty
## You want "s=SYM+SYM+SYM" and "f=<code>" to get the right parameters
## Note that you get different responses from
##      finance.yahoo.com/d/quotes.csv?s=AAPL&f=nab
## and  finance.yahoo.com/d/quotes.csv?s=AAPL&f=n&f=a&f=b
## 
def checkSymbol(symbol):
    yurl = 'http://finance.yahoo.com/d/quotes.csv'
    params = {
        's': symbol,
        'f': 'n0'
        }
    try:
        r = requests.get(yurl, params=params)
    except:
        return ""
    # should be "Company Name"
    # if it is "SYM", then the symbol doesn't exist
    return r.text.strip()

# Symbol(symbol name, private queue, response queue, update time)
# This starts a thread to check for the symbol name periodically.
# It will take commands from the private queue, and act upon them
# And put the response on the response queue.
# If nothing else is going on, sleep for "update time" seconds
class Symbol(threading.Thread):
    global __symbol     # symbol name - e.g. "GS", "GOOG", etc
    global __queue      # input queue
    global __respq      # response queue
    global __lastPrice  # last price is cached
    global __sleepTime  # time to sleep when there's nothing to do
    global __lastupdate # last time updated
    global __lastChange # last price up/down

    # access local variables

    # return symbol used
    def symbol(self):
        return self.__symbol

    def lastchange(self):
        return self.__lastChange

    # return last price (or NaN) retrieved
    def lastprice(self):
        return self.__lastPrice

    # return last update time
    def lastupdate(self):
        return self.__lastupdate

    # return timer/sleep value
    def timer(self):
        return self.__sleepTime

    def __init__(self,symb,queue,rque,sleep):
        super(Symbol,self).__init__()
        print "Initializing symbol:"+symb+", and queue, updates",sleep
        self.__symbol = symb
        self.__queue = queue
        self.__respq = rque
        self.__sleepTime = sleep
        self.__lastPrice = 'NaN'
        self.__lastupdate = 0
        self.__lastChange = 0

    # look up last value for symbol
    def values(self):
        try:
            r = ysq.get_all(self.__symbol)
        except:
            print "Values",self.__symbol,"URLError raised"
            return []
        self.__lastPrice = r['price']  # cache this
        self.__lastupdate = time.time()
        self.__lastChange = r['change']
        return r

    # generate a notify() event - to wake the symbol out of a wait
    def notify(self):
        self.__cv.acquire()
        self.__cv.notify()
        self.__cv.release()

    def run(self):
        # main object loop
        # take commands
        # if no commands, update price
        # wait for __sleepTime
        # This also prints a console message for debugging
        self.__cv = threading.Condition()
        while 1:
            all = self.values()  # first, update self immediately
            # check for empty dictionary return (an error)
            if 'price' in all: 
                print "%s self updates: %s %s %s" % (self.name,
                                                     self.__symbol,
                                                     all['price'],
                                                     all['change'])
            # look for incoming requests
            if self.__queue.qsize() != 0:
                try:
                    action = self.__queue.get()
                    if 'statusQ' in action:
                        # symbol status query
                        print self.name,"recvd statusQ for sym",self.__symbol
                        r = qStatusR.copy()
                        r['statusR'] = self.__lastPrice
                        r['Symbol'] = self.__symbol
                        self.__respq.put(r)
                    elif 'termQ' in action:
                        # symbol should terminate itself
                        print self.name,"recvd termQ for sym",self.__symbol
                        # put answer on queue
                        r = qTerminateR.copy()
                        r['termR'] = True
                        r['Symbol'] = self.__symbol
                        self.__respq.put(r)
                        reaperThread.notify(0)
                        # now close up shop
                        sys.exit()  # should exit thread only
                    else:
                        print "Unknown query:",action
                except Queue.Empty as e:
                    print self.name,"Queue:",self.__symbol,"error:",e.errstr
            else:
                # if no work, wait() for upto sleepTime seconds
                # or until we get a notification
                self.__cv.acquire()
                if self.__queue.qsize() == 0:
                    self.__cv.wait(self.__sleepTime)
                self.__cv.release()
            # update at least every "sleepTime" seconds

# define the reaper - inspect the response queue for messages
class Reaper(threading.Thread):
    global __sleepTime  # time to sleep between reaps

    def __init__(self,timer):
        super(Reaper, self).__init__()
        self.__sleepTime = timer
        print "Reaper runs at",time.time(),"sleeps for",self.__sleepTime

    def notify(self,sleeptime):
        print "Reaper notified"
        if sleeptime != 0:
            time.sleep(sleeptime)
        self.__cv.acquire()
        self.__cv.notify()
        self.__cv.release()

    def run(self):
        # check the shared response queue for messages
        self.__cv = threading.Condition()
        while 1:
            while respQue.qsize() > 0:
                # process response queue
                respQue.lock.acquire()
                r = respQue.get(1, 20) # block for N seconds
                respQue.lock.release()
                # process response
                if 'termR' in r:
                    # reap/cleanup a thread
                    print "Reaper: termR",r['termR'],"from",r['Symbol']
                    del initialThreads[r['Symbol']]
                    del initialQueues[r['Symbol']]
                else:
                    # dunno what symbol this is?
                    print "Reaper: unknown cmd",r
            print "Reaper rescheduling in",self.__sleepTime
            self.__cv.acquire()
            self.__cv.wait(self.__sleepTime)
            print "Reaper awoke"
            self.__cv.release()

##
## From here on, you can really do whatever you want.  The individual
## Symbols will update themselves.
##
## Below, I just fool around putting on a 'statusQuery' object on
## their queues.  Then go and get the data back, and print it out.
## It sleeps again for 10 seconds, then asks them to destroy 
## themselves.
## 

'''
time.sleep(10);
for thread in initialThreads.keys():
    q = qStatusQ;
    q['Symbol'] = thread
    print "Parent: put qStatusQ on",initialThreads[thread].name
    initialQueues[thread].put(q)

# read back answers from the response queue
for thread in initialThreads.keys():
    respQue.lock.acquire()
    r = respQue.get(1, 20) # block for N seconds
    respQue.lock.release()
    print "Parent: thread for",r['Symbol'],"responded",r['statusR']

# now main process just waiting while child threads run
print "Parent process now just waiting..."

time.sleep(10)
for thread in initialThreads.keys():
    print 'Parent: requesting terminate for symbol',thread
    q = qTerminateQ
    q['Symbol'] = thread;
    initialQueues[thread].put(q)

for thread in initialThreads.keys():
    respQue.lock.acquire()
    r = respQue.get(1, 20) # block for N seconds
    respQue.lock.release()
    print "Parent: thread",initialThreads[thread].name,"Symbol",r['Symbol'],"termR responded",r['termR']
    del initialQueues[thread]  # destroy queue
    initialThreads[thread].join(5) # reap thread
'''


##
## Initialize UI components
##

flaqs = Flask(__name__, static_url_path='/static')

# indexSymbols - draw main page, showing symbols, and specifics
#              - allow symbols to be added or deleted
#              - switch to page to check thread details
#              - kill all threads (but not completely shutdown)
@flaqs.route('/')
@flaqs.route('/index')
def indexSymbols():
    r = []
    data = {}
    for sym in initialThreads.keys():
        print "Web: Thread",initialThreads[sym].name,\
            "Symbol",initialThreads[sym].symbol(),\
            "Price",initialThreads[sym].lastprice(),\
            "Update",time.strftime("%H:%M:%S", \
                                       time.localtime(initialThreads[sym].lastupdate()))
        data['tname'] = initialThreads[sym].name
        data['symbol'] = initialThreads[sym].symbol()
        data['lastprice'] = initialThreads[sym].lastprice()
        if initialThreads[sym].lastchange() == 'N/A':
            data['change'] = 0.0
        else:
            data['change'] = float(initialThreads[sym].lastchange())
        if initialThreads[sym].lastupdate() == 0:
            data ['updated'] = "Never"
        else:
            data['updated'] = time.strftime("%H:%M:%S",\
              time.localtime(initialThreads[sym].lastupdate())).strip()
        r.append(data.copy())
    return render_template('flaqs_index.html', syminfo=r)

# delete specific symbol
@flaqs.route('/delsym', methods=['POST'])
def delSym():
    print "Web: delSym called, form:",request.form
    s = request.form['symbol']
    if not s:
        print "Web: *** delSym - symbol not found:",s
        return redirect(url_for('indexSymbols'))
    print "Web: delSym symbol",s
    if s in initialThreads:
        q = qTerminateQ.copy()
        q['Symbol'] = s
        initialQueues[s].put(q)
        initialThreads[s].notify()
        reaperThread.notify(5)  # notify and wait 5 secs
        # watch out, initialThreads[s] may no longer be there
        # use sym name
        print "Web: delSym put termQ and notified",s
    return redirect(url_for('indexSymbols'))

# stop all symbols
@flaqs.route('/stop')
def stopSystem():
    print "Web: stopping everything"
    for s in initialThreads:
        # stop all threads
        q = qTerminateQ.copy()
        q['Symbol'] = s
        initialQueues[s].put(q)
        initialThreads[s].notify()
    # reaper will get to it all eventually
    reaperThread.notify(5)
    return redirect(url_for('indexSymbols'))

# add new symbol
@flaqs.route('/addsym', methods=['POST'])
def addSym():
    if not 'symbol' in request.form:
        # redirect to index
        return redirect(url_for('indexSymbols'))
    s = request.form.get('symbol')
    if s == '':
        return redirect(url_for('indexSymbols'))
    s = s.upper()
    sleepFor = int(request.form.get('timer'))
    # how to tell if symbol is valid?
    r = checkSymbol(s)
    if r == "":
        return redirect(url_for('indexSymbols'))
    if r == '"'+s+'"':
        # bogus symbol
        return redirect(url_for('indexSymbols'))
    # we're sure we have a valid symbol - define it with "timer" wait
    # add symbol thread and queue 
    if not s in initialThreads:
        initialQueues[s] = Queue.Queue(maxsize=10)
        initialThreads[s] = Symbol(s, initialQueues[s], respQue, sleepFor)
        initialThreads[s].name = 'Thread-'+s
        initialThreads[s].start()
    # redirect back to index
    return redirect(url_for('indexSymbols'))

# internal introspection
@flaqs.route('/threadstats')
def threadStats():
    # roll over the threads, showing some current info
    r = []
    data = {}
    for t in initialThreads:
        print "Web: ThreadStats",initialThreads[t].name,\
            "Sym:",initialThreads[t].symbol(),\
            "Price:",initialThreads[t].lastprice(),\
            "Queue:",initialQueues[t].qsize()
        data['tname'] = initialThreads[t].name
        data['symbol'] = initialThreads[t].symbol()
        data['lastprice'] = initialThreads[t].lastprice()
        data['lastupdate'] = time.strftime("%H:%M:%S %Z",time.localtime(initialThreads[t].lastupdate()))
        data['qdepth'] = initialQueues[t].qsize()
        data['timer'] = initialThreads[t].timer()
        r.append(data.copy())
    # iterate over threading() primitive to generate similar data
    s = []
    sdata = {}
    for t in threading.enumerate():
        print "Thrd: ThreadingStats",\
            "Name",t.name
        sdata['sname'] = t.name
        sdata['sident'] = t.ident
        sdata['isalive'] = t.is_alive()
        s.append(sdata.copy())
            
    return render_template('flaqs_threads.html', queueinfo=r, sysinfo=s)

# initalize default threads
# This reads from a data structure for now, but should be on
# some persisted storage
def setupThreads():
    # initialize system queue - used in main process and Reaper()
    global respQue
    respQue = Queue.Queue(maxsize=10)
    respQue.lock = threading.Lock()

    global initialThreads
    global initialQueues
    global reaperThread
    initialThreads = {}
    initialQueues = {}

    # start market threads
    for sym in ['GS','GOOG','YHOO','HMC']:
        initialQueues[sym] = Queue.Queue(maxsize=10)
        initialThreads[sym] = Symbol(sym, initialQueues[sym], respQue, 30 + len(sym)) # pseudo-random sleep
        initialThreads[sym].name = 'Thread-'+sym
        initialThreads[sym].start()

    reaperThread = Reaper(15)
    reaperThread.name = 'Thread-Reaper'
    reaperThread.start()

    # threads now running
    print "Threads running..."

print "Starting server"
if __name__ == '__main__':
    flaqs.before_first_request(setupThreads)  # initalize threads
    port = int(os.getenv('PORT', default=5010))
    flaqs.run(debug=True, use_reloader=False, host='0.0.0.0', port=port) # 
