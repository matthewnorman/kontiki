'''An implementation of the Raft consensus algorithm'''
from kon_tiki.persist import LogEntry
from kon_tiki.fundamentals import median
from twisted.internet import reactor, defer
from twisted.spread import pb
import random

RERUN_RPC = object()


class Server(object):
    '''A Raft participant.

    `peers`: identities for the server's peers

    `persister`: an object that implements the Persist protocol and
    can save and restore to stable storage

    `applyCommand`: callable invoked with the command to apply
     '''

    def __init__(self, cycle, identity, peers, persister, applyCommand,
                 electionTimeoutRange, commitIndex=0, lastApplied=0):
        self.cycle = cycle
        self.identity = identity
        self.peers = peers
        self.persister = persister
        self.applyCommand = applyCommand
        self.electionTimeoutRange = electionTimeoutRange
        self.commitIndex = commitIndex
        self.lastApplied = lastApplied
        self.pending = set()
        self.applyCommitted()

    @classmethod
    def fromServer(cls, electionTimeoutRange, cycle, server):
        server.cancel_all()
        return cls(electionTimeoutRange=electionTimeoutRange,
                   cycle=server.cycle,
                   identity=server.identity,
                   peers=server.peers,
                   persister=server.persister,
                   commitIndex=server.commitIndex,
                   lastApplied=server.lastApplied)

    def defer(self, deferred):
        def remove(result):
            self.pending.remove(deferred)
            return result

        deferred.addCallbacks(remove, remove)
        return deferred

    def cancel_all(self):
        for deferred in self.pending:
            deferred.cancel()

    def applyCommitted(self):
        if self.lastApplied < self.commitIndex:
            for entry in self.persister.logSlice(self.lastApplied,
                                                 self.commitIndex + 1):
                self.applyCommand(entry.command)
            self.lastApplied = self.commitIndex

    def willBecomeFollower(self, term):
        if term > self.persister.currentTerm:
            self.persister.currentTerm = term
            self.cycle.changeState(Follower)
            return True
        return False

    def candidateIdOK(self, candidateId):
        return (self.persister.votedFor is None
                or self.persister.votedFor == candidateId)

    def candidateLogUpToDate(self, lastLogIndex, lastLogTerm):
        # Section 5.4.1
        if self.persister.currentTerm == lastLogTerm:
            return self.persister.lastIndex <= lastLogIndex
        else:
            return self.persister.lastIndexNewerThanTerm(lastLogTerm)

    def remote_appendEntries(self,
                             term, leaderId, prevLogIndex,
                             prevLogTerm, entries, leaderCommit):
        # RPC
        if self.willBecomeFollower(term):
            return RERUN_RPC
        return self.persister.currentTerm, False

    def remote_requestVote(self, term, candidateId, lastLogIndex, lastLogTerm):
        # RPC
        if term < self.persister.currentTerm:
            voteGranted = False
        else:
            voteGranted = (self.candidateIdOK(candidateId)
                           and self.candidateLogUpToDate(lastLogIndex,
                                                         lastLogTerm))
            self.persister.votedFor = candidateId
            self.willBecomeFollower(term)
        return self.persister.currentTerm, voteGranted


class StartsElection(Server):

    def resetElectionTimeout(self):
        if self.votingDeferred is not None:
            self.votingDeferred.cancel()
        self.electionTimeout = random.uniform(*self.electionTimeoutRange)
        d = self.defer(reactor.callLater(self.electionTimeout,
                                         self.cycle.changeState,
                                         Candidate))
        self.becomeCandidateDeferred = d

    def appendEntries(self, *args, **kwargs):
        self.resetElectionTimeout()
        return super(StartsElection, self).appendEntries(*args, **kwargs)


class Follower(Server):
    '''A Raft follower.'''

    leaderId = None

    def remote_appendEntries(self,
                             term, leaderId, prevLogIndex,
                             prevLogTerm, entries, leaderCommit):
        # RPC
        # 1 & 2
        if (term < self.currentTerm
            or not self.persister.indexMatchesTerm(prevLogIndex,
                                                   prevLogTerm)):
            success = False
        else:
            # 3
            new = self.persister.matchLogToEntries(matchAfter=prevLogIndex,
                                                   entries=entries)
            # 4
            self.persister.appendNewEntries(new)

            # 5
            if leaderCommit > self.commitIndex:
                self.commitIndex = min(leaderCommit, self.persister.lastIndex)

            self.applyCommitted()

            self.leaderId = leaderId
            success = True
            self.resetElectionTimeout()

        return self.currentTerm, success

    def remote_command(self, command):
        d = self.defer(self.peers[self.leaderId].pb.callRemote('command',
                                                               command))
        return d


class Candidate(StartsElection):

    def __init__(self, *args, **kwargs):
        super(Candidate, self).__init__(*args, **kwargs)

    def prepareForElection(self):
        self.persister.currentTerm += 1
        self.persister.votedFor = self.identity

    def willBecomeLeader(self, votesSoFar):
        if votesSoFar > len(self.peers) / 2 + 1:
            self.cycle.changeState(Leader)
            return True
        return False

    def conductElection(self):
        self.prepareForElection()
        defer.gatherResults()


class Leader(Server):
    '''A Raft leader.'''

    def __init__(self, *args, **kwargs):
        super(Server, self).__init__(*args, **kwargs)
        self.heartbeatInterval = min(self.electionTimeoutRange[0] - 50, 50)
        self.postElection()
        d = self.defer(reactor.loopingCall(self.heartbeatInterval,
                                           self.broadcastAppendEntries))
        self.heartbeatLoopingCall = d

    def postElection(self):
        lastLogIndex = self.persister.lastLogIndex
        self.nextIndex = dict.fromkeys(self.peers, lastLogIndex + 1)
        self.matchIndex = dict.fromkeys(self.peers, 0)

    def updateCommitIndex(self):
        newCommitIndex = median(self.matchIndex.values())
        if newCommitIndex > self.commitIndex:
            self.commitIndex = newCommitIndex
            return True
        return False

    def receiveAppendEntries(self, result, identity, lastLogIndex):
        term, success = result
        if self.currentTerm < term:
            self.willBecomeFollower()
        elif not success:
            self.nextIndex[identity] -= 1
            # retry
        else:
            self.nextIndex[identity] = lastLogIndex + 1
            self.matchIndex[identity] = lastLogIndex
            if self.updateCommitIndex():
                self.applyCommitted()

    def sendAppendEntries(self, identity, pb):
        prevLogIndex = self.nextIndex[identity] - 1
        allEntries = self.persister.logSlice(start=prevLogIndex, end=None)
        prevLogTerm, entries = allEntries[0], allEntries[1:]
        lastLogIndex = self.persister.lastLogIndex

        d = self.defer(pb.call('appendEntries',
                               term=self.persister.currentTerm,
                               candidateId=self.identity,
                               prevLogIndex=prevLogIndex,
                               prevLogTerm=prevLogTerm,
                               entries=entries))

        d.addCallback(self.receiveAppendEntries,
                      identity=identity,
                      lastLogIndex=lastLogIndex)
        return d

    def broadcastAppendEntries(self):
        for identity, pb in self.peers:
            self.sendAppendEntries(pb)

    def remote_command(self, command):
        self.persister.appendEntries([LogEntry(term=self.persister.currentTerm,
                                               command=command)])
        self.broadcastAppendEntries()
        return True


class ServerCycle(pb.Root):

    def __init__(self, identity, peers, persister, applyCommand,
                 electionTimeoutRange=(.150, .350)):
        self.identity = identity
        self.peers = peers
        self.persister = persister
        self.applyCommand = applyCommand
        self.electionTimeoutRange = electionTimeoutRange
        self.state = Follower(electionTimeoutRange=electionTimeoutRange,
                              cycle=self,
                              identity=identity,
                              peers=peers,
                              persister=persister,
                              applyCommand=applyCommand)

    def changeState(self, newState):
        self.state = newState.fromServer(self.electionTimeoutRange,
                                         cycle=self,
                                         server=self.state)

    def rerun(self, methodName, *args, **kwargs):
        result = RERUN_RPC
        while result is RERUN_RPC:
            method = getattr(self.state, methodName)
            result = method(*args, **kwargs)
        return result

    def remote_appendEntries(self,
                             term, leaderId, prevLogIndex,
                             prevLogTerm, entries, leaderCommit):
        return self.rereun('remote_appendEntries',
                           term, leaderId,
                           prevLogIndex,
                           prevLogTerm,
                           entries,
                           leaderCommit)

    def remote_requestVote(self,
                           term, candidateId, lastLogIndex, lastLogTerm):
        return self.rerun('remote_requestVote',
                          term, candidateId,
                          lastLogIndex, lastLogIndex)

    def remote_command(self, command):
        return self.rerun('remote_command', command)
