import time
import random

try:
    from Queue import PriorityQueue, Empty
except ImportError:
    from queue import PriorityQueue, Empty

class ConnectionSelector(object):
    """
    Simple class used to select a connection from a list of currently live
    connection instances. In init time it is passed a dictionary containing all
    the connections' options which it can then use during the selection
    process. When the `select` method is called it is given a list of
    *currently* live connections to choose from.

    The options dictionary is the one that has been passed to
    :class:`~elasticsearch.Transport` as `hosts` param and the same that is
    used to construct the Connection object itself. When the Connection was
    created from information retrieved from the cluster via the sniffing
    process it will be the dictionary returned by the `host_info_callback`.

    Example of where this would be useful is a zone-aware selector that would
    only select connections from it's own zones and only fall back to other
    connections where there would be none in it's zones.
    """
    def __init__(self, opts):
        """
        :arg opts: dictionary of connection instances and their options
        """
        self.connection_opts = opts

    def select(self, connections):
        """
        Select a connection from the given list.

        :arg connections: list of live connections to choose from
        """
        pass


class RandomSelector(ConnectionSelector):
    """
    Select a connection at random
    """
    def select(self, connections):
        return random.choice(connections)


class RoundRobinSelector(ConnectionSelector):
    """
    Selector using round-robin.
    """
    def __init__(self, opts):
        super(RoundRobinSelector, self).__init__(opts)
        self.rr = -1

    def select(self, connections):
        self.rr += 1
        self.rr %= len(connections)
        return connections[self.rr]


class ConnectionPool(object):
    """
    Container holding the :class:`~elasticsearch.Connection` instances,
    managing the selection process (via a
    :class:`~elasticsearch.ConnectionSelector`) and dead connections.

    It's only interactions are with the :class:`~elasticsearch.Transport` class
    that drives all the actions within `ConnectionPool`.

    Initially connections are stored on the class as a list and, along with the
    connection options, get passed to the `ConnectionSelector` instance for
    future reference.

    Upon each request the `Transport` will ask for a `Connection` via the
    `get_connection` method. If the connection fails (it's `perform_request`
    raises a `ConnectionError`) it will be marked as dead (via `mark_dead`) and
    put on a timeout (if it fails N times in a row the timeout is exponentially
    longer - the formula is `default_timeout * 2 ** (fail_count - 1)`). When
    the timeout is over the connection will be resurrected and returned to the
    live pool. A connection that has been peviously marked as dead and
    succeedes will be marked as live (it's fail count will be deleted).
    """
    def __init__(self, connections, dead_timeout=60, timeout_cutoff=5,
        selector_class=RoundRobinSelector, randomize_hosts=True, **kwargs):
        """
        :arg connections: list of tuples containing the
            :class:`~elasticsearch.Connection` instance and it's options
        :arg dead_timeout: number of seconds a connection should be retired for
            after a failure, increases on consecutive failures
        :arg timeout_cutoff: number of consecutive failures after which the
            timeout doesn't increase
        :arg selector_class: :class:`~elasticsearch.ConnectionSelector`
            subclass to use
        :arg randomize_hosts: shuffle the list of connections upon arrival to
            avoid dog piling effect across processes
        """
        self.connection_opts = connections
        self.connections = [c for (c, opts) in connections]
        # PriorityQueue for thread safety and ease of timeout management
        self.dead = PriorityQueue(len(self.connections))
        self.dead_count = {}

        if randomize_hosts:
            # randomize the connection list to avoid all clients hitting same node
            # after startup/restart
            random.shuffle(self.connections)

        # default timeout after which to try resurrecting a connection
        self.dead_timeout = dead_timeout
        self.timeout_cutoff = timeout_cutoff

        self.selector = selector_class(dict(connections))

    def mark_dead(self, connection, now=None):
        """
        Mark the connection as dead (failed). Remove it from the live pool and
        put it on a timeout.

        :arg connection: the failed instance
        """
        # allow inject for testing purposes
        now = now if now else time.time()
        try:
            self.connections.remove(connection)
        except ValueError:
            # connection not alive or another thread marked it already, ignore
            return
        else:
            dead_count = self.dead_count.get(connection, 0) + 1
            self.dead_count[connection] = dead_count
            timeout = self.dead_timeout * 2 ** min(dead_count - 1, self.timeout_cutoff)
            self.dead.put((now + timeout, connection))

    def mark_live(self, connection):
        """
        Mark connection as healthy after a resurrection. Resets the fail
        counter for the connection.

        :arg connection: the connection to redeem
        """
        try:
            del self.dead_count[connection]
        except KeyError:
            # race condition, safe to ignore
            pass

    def resurrect(self, force=False):
        """
        Attempt to resurrect a connection from the dead pool. It will try to
        locate one (not all) eligible (it's timeout is over) connection to
        return to th live pool.

        :arg force: resurrect a connection even if there is none eligible (used
            when we have no live connections)

        """
        # no dead connections
        if self.dead.empty():
            return

        try:
            # retrieve a connection to check
            timeout, connection = self.dead.get(block=False)
        except Empty:
            # other thread has been faster and the queue is now empty
            return

        if not force and timeout > time.time():
            # return it back if not eligible and not forced
            self.dead.put((timeout, connection))
            return

        # either we were forced or the connection is elligible to be retried
        self.connections.append(connection)

    def get_connection(self):
        """
        Return a connection from the pool using the `ConnectionSelector`
        instance.

        It tries to resurrect eligible connections, forces a resurrection when
        no connections are availible and passes the list of live connections to
        the selector instance to choose from.

        Returns a connection instance and it's current fail count.
        """
        self.resurrect()

        # no live nodes, resurrect one by force
        if not self.connections:
            self.resurrect(True)

        connection = self.selector.select(self.connections)

        return connection


