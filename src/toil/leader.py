# Copyright (C) 2015-2016 Regents of the University of California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
The leader script (of the leader/worker pair) for running jobs.
"""
from __future__ import absolute_import

import cPickle
import json
import logging
import time
from Queue import Queue, Empty
from collections import namedtuple
from threading import Thread, Event

from bd2k.util.expando import Expando

from toil import resolveEntryPoint
from toil.jobStores.abstractJobStore import NoSuchJobException
from toil.lib.bioio import getTotalCpuTime
from toil.provisioners.clusterScaler import ClusterScaler

logger = logging.getLogger( __name__ )

####################################################
##Stats/logging aggregation
####################################################

class StatsAndLogging( object ):
    """
    Class manages a thread that aggregates statistics and logging information on a toil run.
    """

    def __init__(self, jobStore):
        self._stop = Event()
        self._worker = Thread(target=self.statsAndLoggingAggregator,
                              args=(jobStore, self._stop))
    
    def start(self):
        """
        Start the stats and logging thread.
        """
        self._worker.start()

    @staticmethod
    def statsAndLoggingAggregator(jobStore, stop):
        """
        The following function is used for collating stats/reporting log messages from the workers.
        Works inside of a thread, collates as long as the stop flag is not True.
        """
        #  Overall timing
        startTime = time.time()
        startClock = getTotalCpuTime()

        def callback(fileHandle):
            stats = json.load(fileHandle, object_hook=Expando)
            try:
                logs = stats.workers.logsToMaster
            except AttributeError:
                # To be expected if there were no calls to logToMaster()
                pass
            else:
                for message in logs:
                    logger.log(int(message.level),
                               'Got message from job at time %s: %s',
                               time.strftime('%m-%d-%Y %H:%M:%S'), message.text)
            try:
                logs = stats.logs
            except AttributeError:
                pass
            else:
                def logWithFormatting(jobStoreID, jobLogs):
                    logFormat = '\n%s    ' % jobStoreID
                    logger.debug('Received Toil worker log. Disable debug level '
                                 'logging to hide this output\n%s', logFormat.join(jobLogs))
                # we may have multiple jobs per worker
                # logs[0] is guaranteed to exist in this branch
                currentJobStoreID = logs[0].jobStoreID
                jobLogs = []
                for log in logs:
                    jobStoreID = log.jobStoreID
                    if jobStoreID == currentJobStoreID:
                        # aggregate all the job's logs into 1 list
                        jobLogs.append(log.text)
                    else:
                        # we have reached the next job, output the aggregated logs and continue
                        logWithFormatting(currentJobStoreID, jobLogs)
                        jobLogs = []
                        currentJobStoreID = jobStoreID
                # output the last job's logs
                logWithFormatting(currentJobStoreID, jobLogs)

        while True:
            # This is a indirect way of getting a message to the thread to exit
            if stop.is_set():
                jobStore.readStatsAndLogging(callback)
                break
            if jobStore.readStatsAndLogging(callback) == 0:
                time.sleep(0.5)  # Avoid cycling too fast

        # Finish the stats file
        text = json.dumps(dict(total_time=str(time.time() - startTime),
                               total_clock=str(getTotalCpuTime() - startClock)))
        jobStore.writeStatsAndLogging(text)

    def check(self):
        """
        Check on the stats and logging aggregator.
        :raise RuntimeError: If the underlying thread has quit.
        """
        if not self._worker.is_alive():
            raise RuntimeError("Stats and logging thread has quit")

    def shutdown(self):
        """
        Finish up the stats/logging aggregation thread
        """
        logger.info('Waiting for stats and logging collator thread to finish ...')
        startTime = time.time()
        self._stop.set()
        self._worker.join()
        logger.info('... finished collating stats and logs. Took %s seconds', time.time() - startTime)
        # in addition to cleaning on exceptions, onError should clean if there are any failed jobs

##########################################
#Class to represent the state of the toil in memory. Loads this
#representation from a toil jobStore.
##########################################

class ToilState( object ):
    """
    Represents a snapshot of the jobs in the jobStore.
    """
    def __init__( self, jobStore, rootJob, jobCache=None):
        """
        Loads the state from the jobStore, using the rootJob 
        as the source of the job graph.
        
        The jobCache is a map from jobStoreIDs to jobWrappers or None. Is used to
        speed up the building of the state.
        
        :param toil.jobStores.abstractJobStore.AbstractJobStore jobStore 
        :param toil.jobWrapper.JobWrapper rootJob
        """
        # This is a hash of jobs, referenced by jobStoreID, to their predecessor jobs.
        self.successorJobStoreIDToPredecessorJobs = { }
        
        # Hash of jobStoreIDs to counts of numbers of successors issued.
        # There are no entries for jobs
        # without successors in this map.
        self.successorCounts = { }

        # This is a hash of service jobs, referenced by jobStoreID, to their predecessor job
        self.serviceJobStoreIDToPredecessorJob = { }

        # Hash of jobStoreIDs to maps of services issued for the job
        # Each for job, the map is a dictionary of service jobStoreIDs
        # to the flags used to communicate the with service
        self.servicesIssued = { }
        
        # Jobs that are ready to be processed
        self.updatedJobs = set( )
        
        # The set of totally failed jobs - this needs to be filtered at the
        # end to remove jobs that were removed by checkpoints
        self.totalFailedJobs = set()
        
        # Jobs (as jobStoreIDs) with successors that have totally failed
        self.hasFailedSuccessors = set()
        
        # The set of successors of failed jobs as a set of jobStoreIds
        self.failedSuccessors = set()
        
        # Set of jobs that have multiple predecessors that have one or more predecessors
        # finished, but not all of them. This acts as a cache for these jobs.
        # Stored as hash from jobStoreIDs to jobWrappers
        self.jobsToBeScheduledWithMultiplePredecessors = {}
        
        ##Algorithm to build this information
        logger.info("(Re)building internal scheduler state")
        self._buildToilState(rootJob, jobStore, jobCache)

    def _buildToilState(self, jobWrapper, jobStore, jobCache=None):
        """
        Traverses tree of jobs from the root jobWrapper (rootJob) building the
        ToilState class.

        If jobCache is passed, it must be a dict from job ID to JobWrapper
        object. Jobs will be loaded from the cache (which can be downloaded from
        the jobStore in a batch) instead of piecemeal when recursed into.
        """

        def getJob(jobId):
            if jobCache is not None:
                try:
                    return jobCache[jobId]
                except ValueError:
                    return jobStore.load(jobId)
            else:
                return jobStore.load(jobId)

        # If the jobWrapper has a command, is a checkpoint, has services or is ready to be
        # deleted it is ready to be processed
        if (jobWrapper.command is not None
            or jobWrapper.checkpoint is not None
            or len(jobWrapper.services) > 0
            or len(jobWrapper.stack) == 0):
            logger.debug('Found job to run: %s, with command: %s, with checkpoint: %s, '
                         'with  services: %s, with stack: %s', jobWrapper.jobStoreID,
                         jobWrapper.command is not None, jobWrapper.checkpoint is not None,
                         len(jobWrapper.services) > 0, len(jobWrapper.stack) == 0)
            self.updatedJobs.add((jobWrapper, 0))

            if jobWrapper.checkpoint is not None:
                jobWrapper.command = jobWrapper.checkpoint

        else: # There exist successors
            logger.debug("Adding job: %s to the state with %s successors" % (jobWrapper.jobStoreID, len(jobWrapper.stack[-1])))
            
            # Record the number of successors
            self.successorCounts[jobWrapper.jobStoreID] = len(jobWrapper.stack[-1])
            
            def processSuccessorWithMultiplePredecessors(successorJobWrapper):
                # If jobWrapper job is not reported as complete by the successor
                if jobWrapper.jobStoreID not in successorJobWrapper.predecessorsFinished:
                    
                    # Update the sucessor's status to mark the predecessor complete
                    successorJobWrapper.predecessorsFinished.add(jobWrapper.jobStoreID)
            
                # If the successor has no predecessors to finish
                assert len(successorJobWrapper.predecessorsFinished) <= successorJobWrapper.predecessorNumber
                if len(successorJobWrapper.predecessorsFinished) == successorJobWrapper.predecessorNumber:
                    
                    # It is ready to be run, so remove it from the cache
                    self.jobsToBeScheduledWithMultiplePredecessors.pop(successorJobStoreID)
                    
                    # Recursively consider the successor
                    self._buildToilState(successorJobWrapper, jobStore, jobCache=jobCache)
            
            # For each successor
            for successorJobStoreTuple in jobWrapper.stack[-1]:
                successorJobStoreID = successorJobStoreTuple[0]
                
                # If the successor jobWrapper does not yet point back at a
                # predecessor we have not yet considered it
                if successorJobStoreID not in self.successorJobStoreIDToPredecessorJobs:

                    # Add the job as a predecessor
                    self.successorJobStoreIDToPredecessorJobs[successorJobStoreID] = [jobWrapper]
                    
                    # If predecessorJobStoreID is not None then the successor has multiple predecessors
                    predecessorJobStoreID = successorJobStoreTuple[-1]
                    if predecessorJobStoreID != None: 
                        
                        # We load the successor job
                        successorJobWrapper =  getJob(successorJobStoreID)
                        
                        # We put the successor job in the cache of successor jobs with multiple predecessors
                        assert successorJobStoreID not in self.jobsToBeScheduledWithMultiplePredecessors
                        self.jobsToBeScheduledWithMultiplePredecessors[successorJobStoreID] = successorJobWrapper
                        
                        # Process successor
                        processSuccessorWithMultiplePredecessors(successorJobWrapper)
                            
                    else:
                        # The successor has only the jobWrapper job as a predecessor so
                        # recursively consider the successor
                        self._buildToilState(getJob(successorJobStoreID), jobStore, jobCache=jobCache)
                
                else:
                    # We've already seen the successor
                    
                    # Add the job as a predecessor
                    assert jobWrapper not in self.successorJobStoreIDToPredecessorJobs[successorJobStoreID]
                    self.successorJobStoreIDToPredecessorJobs[successorJobStoreID].append(jobWrapper) 
                    
                    # If the successor has multiple predecessors
                    if successorJobStoreID in self.jobsToBeScheduledWithMultiplePredecessors:
                        
                        # Get the successor from cache
                        successorJobWrapper = self.jobsToBeScheduledWithMultiplePredecessors[successorJobStoreID]
                        
                        # Process successor
                        processSuccessorWithMultiplePredecessors(successorJobWrapper)
        
##########################################
# Class that manages a thread that schedules service jobs
##########################################

class ServiceManager( object ):
    """
    Manages the scheduling of services.
    """
    def __init__(self, jobStore):
        """
        :param toil.jobStores.abstractJobStore.AbstractJobStore jobStore 
        """
        self.jobStore = jobStore

        self.jobWrappersWithServicesBeingStarted = set()

        self._terminate = Event() # This is used to terminate the thread associated
        # with the service manager

        self._jobWrappersWithServicesToStart = Queue() # This is the input queue of
        # jobWrappers that have services that need to be started

        self._jobWrappersWithServicesThatHaveStarted = Queue() # This is the output queue
        # of jobWrappers that have services that are already started

        self._serviceJobWrappersToStart = Queue() # This is the queue of services for the
        # batch system to start

        self.serviceJobsIssuedToServiceManager = 0 # The number of jobs the service manager
        # is scheduling

        # Start a thread that starts the services of jobWrappers in the
        # jobsWithServicesToStart input queue and puts the jobWrappers whose services
        # are running on the jobWrappersWithServicesThatHaveStarted output queue
        self._serviceStarter = Thread(target=self._startServices,
                                     args=(self._jobWrappersWithServicesToStart,
                                           self._jobWrappersWithServicesThatHaveStarted,
                                           self._serviceJobWrappersToStart, self._terminate,
                                           self.jobStore))
        
    def start(self):
        """
        Start the service scheduling thread.
        """
        self._serviceStarter.start()

    def scheduleServices(self, jobWrapper):
        """
        Schedule the services of a job asynchronously.
        When the job's services are running the jobWrapper for the job will
        be returned by toil.leader.ServiceManager.getJobWrappersWhoseServicesAreRunning.

        :param toil.jobWrapper.JobWrapper jobWrapper: wrapper of job with services to schedule.
        """
        # Add jobWrapper to set being processed by the service manager
        self.jobWrappersWithServicesBeingStarted.add(jobWrapper)

        # Add number of jobs managed by ServiceManager
        self.serviceJobsIssuedToServiceManager += sum(map(len, jobWrapper.services)) + 1 # The plus one accounts for the root job

        # Asynchronously schedule the services
        self._jobWrappersWithServicesToStart.put(jobWrapper)

    def getJobWrapperWhoseServicesAreRunning(self, maxWait):
        """
        :param float maxWait: Time in seconds to wait to get a jobWrapper before returning
        :return: a jobWrapper added to scheduleServices whose services are running, or None if
        no such job is available.
        :rtype: JobWrapper
        """
        try:
            jobWrapper = self._jobWrappersWithServicesThatHaveStarted.get(timeout=maxWait)
            self.jobWrappersWithServicesBeingStarted.remove(jobWrapper)
            assert self.serviceJobsIssuedToServiceManager >= 0
            self.serviceJobsIssuedToServiceManager -= 1
            return jobWrapper
        except Empty:
            return None

    def getServiceJobsToStart(self, maxWait):
        """
        :param float maxWait: Time in seconds to wait to get a job before returning.
        :return: a tuple of (serviceJobStoreID, memory, cores, disk, ..) representing
        a service job to start.
        :rtype: (str, float, float, float)
        """
        try:
            jobTuple = self._serviceJobWrappersToStart.get(timeout=maxWait)
            assert self.serviceJobsIssuedToServiceManager >= 0
            self.serviceJobsIssuedToServiceManager -= 1
            return jobTuple
        except Empty:
            return None

    def killServices(self, services, error=False):
        """
        :param dict services: Maps service jobStoreIDs to the communication flags for the service
        """
        for serviceJobStoreID in services:
            startJobStoreID, terminateJobStoreID, errorJobStoreID = services[serviceJobStoreID]
            if error:
                self.jobStore.deleteFile(errorJobStoreID)
            self.jobStore.deleteFile(terminateJobStoreID)

    def check(self):
        """
        Check on the service manager thread.
        :raise RuntimeError: If the underlying thread has quit.
        """
        if not self._serviceStarter.is_alive():
            raise RuntimeError("Service manager has quit")

    def shutdown(self):
        """
        Cleanly terminate worker threads starting and killing services. Will block
        until all services are started and blocked.
        """
        logger.info('Waiting for service manager thread to finish ...')
        startTime = time.time()
        self._terminate.set()
        self._serviceStarter.join()
        logger.info('... finished shutting down the service manager. Took %s seconds', time.time() - startTime)

    @staticmethod
    def _startServices(jobWrappersWithServicesToStart,
                       jobWrappersWithServicesThatHaveStarted,
                       serviceJobsToStart,
                       terminate, jobStore):
        """
        Thread used to schedule services.
        """
        while True:
            try:
                # Get a jobWrapper with services to start, waiting a short period
                jobWrapper = jobWrappersWithServicesToStart.get(timeout=1.0)
            except:
                # Check if the thread should quit
                if terminate.is_set():
                    logger.debug('Received signal to quit starting services.')
                    break
                continue

            if jobWrapper is None: # Nothing was ready, loop again
                continue

            # Start the service jobs in batches, waiting for each batch
            # to become established before starting the next batch
            for serviceJobList in jobWrapper.services:
                for serviceJobStoreID, memory, cores, disk, startJobStoreID, terminateJobStoreID, errorJobStoreID in serviceJobList:
                    logger.debug("Service manager is starting service job: %s, start ID: %s", serviceJobStoreID, startJobStoreID)
                    assert jobStore.fileExists(startJobStoreID)
                    # At this point the terminateJobStoreID and errorJobStoreID could have been deleted!
                    serviceJobsToStart.put((serviceJobStoreID, memory, cores, disk))

                # Wait until all the services of the batch are running
                for serviceTuple in serviceJobList:
                    while jobStore.fileExists(serviceTuple[4]):
                        # Sleep to avoid thrashing
                        time.sleep(1.0)

                        # Check if the thread should quit
                        if terminate.is_set():
                            logger.debug('Received signal to quit starting services.')
                            break

            # Add the jobWrapper to the output queue of jobs whose services have been started
            jobWrappersWithServicesThatHaveStarted.put(jobWrapper)

####################################################
# Exception thrown by the Leader class when one or more jobs fails
#################################################### 
           
class FailedJobsException( Exception ):
    def __init__(self, jobStoreLocator, failedJobs, jobStore):
        msg = "The job store '%s' contains %i failed jobs" % (jobStoreLocator, len(failedJobs))
        try:
            msg += ": %s" % ", ".join(failedJobs)
            for failedID in failedJobs:
                job = jobStore.load(failedID)
                if job.logJobStoreFileID:
                    msg += "\n=========> Failed job %s\n" % failedID
                    with job.getLogFileHandle(jobStore) as fH:
                        msg += fH.read()
                    msg += "<=========\n"
        # catch failures to prepare more complex details and only return the basics
        except:
            logger.exception('Exception when compiling information about failed jobs')
        super( FailedJobsException, self ).__init__(msg)
        self.jobStoreLocator = jobStoreLocator
        self.numberOfFailedJobs = len(failedJobs)
            
####################################################
##Following class represents the leader
####################################################

# Represents a job and its requirements as issued to the batch system
IssuedJob = namedtuple("IssuedJob", "jobStoreID memory cores disk preemptable")
              
class Leader:
    """ Class that encapsulates the logic of the leader.
    """
    def __init__(self, config, batchSystem, provisioner, jobStore, rootJobWrapper, jobCache=None):
        """    
        :param toil.common.Config config:
        :param toil.batchSystems.abstractBatchSystem.AbstractBatchSystem batchSystem:
        :param toil.provisioners.abstractProvisioner.AbstractProvisioner provisioner
        :param toil.jobStores.abstractJobStore.AbstractJobStore jobStore:
        :param toil.jobWrapper.JobWrapper rootJobWrapper
        
        If jobCache is passed, it must be a dict from job ID to pre-existing
        JobWrapper objects. Jobs will be loaded from the cache (which can be
        downloaded from the jobStore in a batch) during the construction of the ToilState object.
        """
        # Object containing parameters for the run
        self.config = config
        
        # The job store
        self.jobStore = jobStore
        self.jobStoreLocator = config.jobStore
        
        # Get a snap shot of the current state of the jobs in the jobStore
        self.toilState = ToilState(jobStore, rootJobWrapper, jobCache=jobCache)
        logger.info("Found %s jobs to start and %i jobs with successors to run",
                        len(self.toilState.updatedJobs), len(self.toilState.successorCounts))
        
        # Batch system
        self.batchSystem = batchSystem
        assert len(self.batchSystem.getIssuedBatchJobIDs()) == 0 #Batch system must start with no active jobs!
        logger.info("Checked batch system has no running jobs and no updated jobs")
        
        # Map of batch system IDs to IsseudJob tuples
        self.jobBatchSystemIDToIssuedJob = {}
        
        # Number of jobs currently being run by batch system
        self.jobsIssued = 0
        self._preemptableJobsIssued = 0
        
        # Hash to store number of times a job is lost by the batch system,
        # used to decide if to reissue an apparently missing job
        self.reissueMissingJobs_missingHash = {} 
        
        # Class used to create/destroy nodes in the cluster, may be None if
        # using a statically defined cluster
        self.provisioner = provisioner
        
        # Create cluster scaling thread if the provisioner is not None
        self.clusterScaler = None if self.provisioner is None else ClusterScaler(self.provisioner, self, self.config)
        
        # A service manager thread to start and terminate services
        self.serviceManager = ServiceManager(jobStore)
        
        # A thread to manage the aggregation of statistics and logging from the run
        self.statsAndLogging = StatsAndLogging(self.jobStore)
           
    def run(self):
        """
        This runs the leader process to issue and manage jobs.
    
        :raises: toil.leader.FailedJobsException if at the end of function their remain \
        failed jobs
        
        :return: The return value of the root job's run function.
        :rtype: Any
        """
        # Start service manager thread 
        self.serviceManager.start()
        try:
            
            # Start the stats/logging aggregation thread
            self.statsAndLogging.start()
            try:
                
                # Create cluster scaling processes if not None
                if self.clusterScaler != None:
                    self.clusterScaler.start()
                
                try:
                    # Run the main loop
                    self.innerLoop()
                    
                finally:
                    if self.clusterScaler is not None:
                        logger.info('Waiting for workers to shutdown')
                        startTime = time.time()
                        self.clusterScaler.shutdown()
                        logger.info('Worker shutdown complete in %s seconds', time.time() - startTime)
    
            finally:
                # Ensure the stats and logging thread is properly shutdown
                self.statsAndLogging.shutdown()
                
        finally:
            # Ensure service manager thread is properly shutdown
            self.serviceManager.shutdown()
    
        # Filter any failed jobs
        self.toilState.totalFailedJobs = set(filter(self.jobStore.exists, self.toilState.totalFailedJobs))
    
        logger.info("Finished toil run %s" %
                     ("successfully" if len(self.toilState.totalFailedJobs) == 0 else ("with %s failed jobs" % len(self.toilState.totalFailedJobs))))
        
        if len(self.toilState.totalFailedJobs):
            logger.info("Failed jobs at end of the run: %s", self.toilState.totalFailedJobs)
    
        # Cleanup
        if len(self.toilState.totalFailedJobs) > 0:
            raise FailedJobsException(self.config.jobStore, self.toilState.totalFailedJobs, self.jobStore)
    
        # Parse out the return value from the root job
        with self.jobStore.readSharedFileStream('rootJobReturnValue') as fH:
            try:
                return cPickle.load(fH)
            except EOFError:
                logger.exception('Failed to unpickle root job return value')
                raise FailedJobsException(self.config.jobStore, self.toilState.totalFailedJobs, self.jobStore)
    
    def innerLoop(self):
        """
        The main loop for processing jobs by the leader.
        """
    
        # Sets up the timing of the jobWrapper rescuing method
        timeSinceJobsLastRescued = time.time()
    
        logger.info("Starting the main loop")
        while True:
            # Process jobs that are ready to be scheduled/have successors to schedule
            if len(self.toilState.updatedJobs) > 0:
                logger.debug('Built the jobs list, currently have %i jobs to update and %i jobs issued',
                             len(self.toilState.updatedJobs), self.getNumberOfJobsIssued())
    
                updatedJobs = self.toilState.updatedJobs # The updated jobs to consider below
                self.toilState.updatedJobs = set() # Resetting the list for the next set
    
                for jobWrapper, resultStatus in updatedJobs:
    
                    logger.debug('Updating status of job: %s with result status: %s',
                                 jobWrapper.jobStoreID, resultStatus)
    
                    # This stops a job with services being issued by the serviceManager from
                    # being considered further in this loop. This catch is necessary because
                    # the job's service's can fail while being issued, causing the job to be
                    # added to updated jobs.
                    if jobWrapper in self.serviceManager.jobWrappersWithServicesBeingStarted:
                        logger.debug("Got a job to update which is still owned by the service "
                                     "manager: %s", jobWrapper.jobStoreID)
                        continue
    
                    # If some of the jobs successors failed then either fail the job
                    # or restart it if it has retries left and is a checkpoint job
                    if jobWrapper.jobStoreID in self.toilState.hasFailedSuccessors:
    
                        # If the job has services running, signal for them to be killed
                        # once they are killed then the jobWrapper will be re-added to the
                        # updatedJobs set and then scheduled to be removed
                        if jobWrapper.jobStoreID in self.toilState.servicesIssued:
                            logger.debug("Telling job: %s to terminate its services due to successor failure",
                                         jobWrapper.jobStoreID)
                            self.serviceManager.killServices(self.toilState.servicesIssued[jobWrapper.jobStoreID],
                                                        error=True)
    
                        # If the job has non-service jobs running wait for them to finish
                        # the job will be re-added to the updated jobs when these jobs are done
                        elif jobWrapper.jobStoreID in self.toilState.successorCounts:
                            logger.debug("Job: %s with failed successors still has successor jobs running", jobWrapper.jobStoreID)
                            continue
    
                        # If the job is a checkpoint and has remaining retries then reissue it.
                        elif jobWrapper.checkpoint is not None and jobWrapper.remainingRetryCount > 0:
                            logger.warn('Job: %s is being restarted as a checkpoint after the total '
                                        'failure of jobs in its subtree.', jobWrapper.jobStoreID)
                            self.issueJob(jobWrapper.jobStoreID,
                                                memory=jobWrapper.memory,
                                                cores=jobWrapper.cores,
                                                disk=jobWrapper.disk,
                                                preemptable=jobWrapper.preemptable)
                            
                        else: # Mark it totally failed
                            logger.debug("Job %s is being processed as completely failed", jobWrapper.jobStoreID)
                            self.processTotallyFailedJob(jobWrapper)
    
                    # If the jobWrapper has a command it must be run before any successors.
                    # Similarly, if the job previously failed we rerun it, even if it doesn't have a
                    # command to run, to eliminate any parts of the stack now completed.
                    elif jobWrapper.command is not None or resultStatus != 0:
                        isServiceJob = jobWrapper.jobStoreID in self.toilState.serviceJobStoreIDToPredecessorJob
    
                        # If the job has run out of retries or is a service job whose error flag has
                        # been indicated, fail the job.
                        if (jobWrapper.remainingRetryCount == 0
                            or isServiceJob and not self.jobStore.fileExists(jobWrapper.errorJobStoreID)):
                            self.processTotallyFailedJob(jobWrapper)
                            logger.warn("Job: %s is completely failed", jobWrapper.jobStoreID)
                        else:
                            # Otherwise try the job again
                            self.issueJob(jobWrapper.jobStoreID, jobWrapper.memory,
                                                jobWrapper.cores, jobWrapper.disk, jobWrapper.preemptable)
    
                    # If the job has services to run, which have not been started, start them
                    elif len(jobWrapper.services) > 0:
                        # Build a map from the service jobs to the job and a map
                        # of the services created for the job
                        assert jobWrapper.jobStoreID not in self.toilState.servicesIssued
                        self.toilState.servicesIssued[jobWrapper.jobStoreID] = {}
                        for serviceJobList in jobWrapper.services:
                            for serviceTuple in serviceJobList:
                                serviceID = serviceTuple[0]
                                assert serviceID not in self.toilState.serviceJobStoreIDToPredecessorJob
                                self.toilState.serviceJobStoreIDToPredecessorJob[serviceID] = jobWrapper
                                self.toilState.servicesIssued[jobWrapper.jobStoreID][serviceID] = serviceTuple[4:7]
    
                        # Use the service manager to start the services
                        self.serviceManager.scheduleServices(jobWrapper)
    
                        logger.debug("Giving job: %s to service manager to schedule its jobs", jobWrapper.jobStoreID)
    
                    # There exist successors to run
                    elif len(jobWrapper.stack) > 0:
                        assert len(jobWrapper.stack[-1]) > 0
                        logger.debug("Job: %s has %i successors to schedule",
                                     jobWrapper.jobStoreID, len(jobWrapper.stack[-1]))
                        
                        #Record the number of successors that must be completed before
                        #the jobWrapper can be considered again
                        assert jobWrapper.jobStoreID not in self.toilState.successorCounts
                        self.toilState.successorCounts[jobWrapper.jobStoreID] = len(jobWrapper.stack[-1])
                        
                        #List of successors to schedule
                        successors = []
                        
                        #For each successor schedule if all predecessors have been completed
                        for successorJobStoreID, memory, cores, disk, preemptable, predecessorID in jobWrapper.stack[-1]:
                            #Build map from successor to predecessors.
                            if successorJobStoreID not in self.toilState.successorJobStoreIDToPredecessorJobs:
                                self.toilState.successorJobStoreIDToPredecessorJobs[successorJobStoreID] = []
                            self.toilState.successorJobStoreIDToPredecessorJobs[successorJobStoreID].append(jobWrapper)
                            #Case that the jobWrapper has multiple predecessors
                            if predecessorID is not None:
                                logger.debug("Successor job: %s of job: %s has multiple predecessors" % (successorJobStoreID, jobWrapper.jobStoreID))
                                
                                # Get the successor job, using a cache 
                                # (if the successor job has already been seen it will be in this cache, 
                                # but otherwise put it in the cache)
                                if successorJobStoreID not in self.toilState.jobsToBeScheduledWithMultiplePredecessors:
                                    self.toilState.jobsToBeScheduledWithMultiplePredecessors[successorJobStoreID] = self.jobStore.load(successorJobStoreID)      
                                successorJobWrapper = self.toilState.jobsToBeScheduledWithMultiplePredecessors[successorJobStoreID]
                                
                                #Add the jobWrapper job as a finished predecessor to the successor 
                                successorJobWrapper.predecessorsFinished.add(predecessorID)
    
                                # If the successor is in the set of successors of failed jobs 
                                if successorJobStoreID in self.toilState.failedSuccessors:
                                    logger.debug("Successor job: %s of job: %s has failed predecessors" % (successorJobStoreID, jobWrapper.jobStoreID))
                                    
                                    # Add the job to the set having failed successors
                                    self.toilState.hasFailedSuccessors.add(jobWrapper.jobStoreID)
                                    
                                    # Reduce active successor count and remove the successor as an active successor of the job
                                    self.toilState.successorCounts[jobWrapper.jobStoreID] -= 1
                                    assert self.toilState.successorCounts[jobWrapper.jobStoreID] >= 0
                                    self.toilState.successorJobStoreIDToPredecessorJobs[successorJobStoreID].remove(jobWrapper)
                                    if len(self.toilState.successorJobStoreIDToPredecessorJobs[successorJobStoreID]) == 0:
                                        self.toilState.successorJobStoreIDToPredecessorJobs.pop(successorJobStoreID)
                                    
                                    # If the job now has no active successors add to active jobs
                                    # so it can be processed as a job with failed successors
                                    if self.toilState.successorCounts[jobWrapper.jobStoreID] == 0:
                                        logger.debug("Job: %s has no successors to run and some are failed, adding to list of jobs with failed successors" % jobWrapper.jobStoreID)
                                        self.toilState.successorCounts.pop(jobWrapper.jobStoreID)
                                        self.toilState.updatedJobs.add((jobWrapper, 0))
                                        continue
    
                                # If the successor job's predecessors have all not all completed then
                                # ignore the jobWrapper as is not yet ready to run
                                assert len(successorJobWrapper.predecessorsFinished) <= successorJobWrapper.predecessorNumber
                                if len(successorJobWrapper.predecessorsFinished) < successorJobWrapper.predecessorNumber:
                                    continue
                                else:
                                    # Remove the successor job from the cache
                                    self.toilState.jobsToBeScheduledWithMultiplePredecessors.pop(successorJobStoreID)
                            
                            # Add successor to list of successors to schedule   
                            successors.append((successorJobStoreID, memory, cores, disk, preemptable))
                        self.issueJobs(successors)
    
                    elif jobWrapper.jobStoreID in self.toilState.servicesIssued:
                        logger.debug("Telling job: %s to terminate its services due to the successful completion of its successor jobs",
                                    jobWrapper.jobStoreID)
                        self.serviceManager.killServices(self.toilState.servicesIssued[jobWrapper.jobStoreID], error=False)
    
                    #There are no remaining tasks to schedule within the jobWrapper, but
                    #we schedule it anyway to allow it to be deleted.
    
                    #TODO: An alternative would be simple delete it here and add it to the
                    #list of jobs to process, or (better) to create an asynchronous
                    #process that deletes jobs and then feeds them back into the set
                    #of jobs to be processed
                    else:
                        # Remove the job
                        if jobWrapper.remainingRetryCount > 0:
                            self.issueJob(jobWrapper.jobStoreID,
                                                    memory=self.config.defaultMemory,
                                                    cores=self.config.defaultCores,
                                                    disk=self.config.defaultDisk,
                                                    # We allow this cleanup to potentially occur on a
                                                    # preemptable instance.
                                                    preemptable=True)
                            logger.debug("Job: %s is empty, we are scheduling to clean it up", jobWrapper.jobStoreID)
                        else:
                            self.processTotallyFailedJob(jobWrapper)
                            logger.warn("Job: %s is empty but completely failed - something is very wrong", jobWrapper.jobStoreID)
    
            # The exit criterion
            if len(self.toilState.updatedJobs) == 0 and self.getNumberOfJobsIssued() == 0 and self.serviceManager.serviceJobsIssuedToServiceManager == 0:
                logger.info("No jobs left to run so exiting.")
                break
    
            # Start any service jobs available from the service manager
            while True:
                serviceJobTuple = self.serviceManager.getServiceJobsToStart(0)
                # Stop trying to get jobs when function returns None
                if serviceJobTuple is None:
                    break
                serviceJobStoreID, memory, cores, disk = serviceJobTuple
                logger.debug('Launching service job: %s', serviceJobStoreID)
                # This loop issues the jobs to the batch system because the batch system is not
                # thread-safe. FIXME: don't understand this comment
                self.issueJob(serviceJobStoreID, memory, cores, disk, False)
    
            # Get jobs whose services have started
            while True:
                jobWrapper = self.serviceManager.getJobWrapperWhoseServicesAreRunning(0)
                if jobWrapper is None: # Stop trying to get jobs when function returns None
                    break
                logger.debug('Job: %s has established its services.', jobWrapper.jobStoreID)
                jobWrapper.services = []
                self.toilState.updatedJobs.add((jobWrapper, 0))
    
            # Gather any new, updated jobWrapper from the batch system
            updatedJob =self.batchSystem.getUpdatedBatchJob(2)
            if updatedJob is not None:
                jobBatchSystemID, result, wallTime = updatedJob
                if self.hasJob(jobBatchSystemID):
                    if result == 0:
                        logger.debug('Batch system is reporting that the jobWrapper with '
                                     'batch system ID: %s and jobWrapper store ID: %s ended successfully',
                                     jobBatchSystemID, self.getJob(jobBatchSystemID))
                    else:
                        logger.warn('Batch system is reporting that the jobWrapper with '
                                    'batch system ID: %s and jobWrapper store ID: %s failed with exit value %i',
                                    jobBatchSystemID, self.getJob(jobBatchSystemID), result)
                    self.processFinishedJob(jobBatchSystemID, result, wallTime=wallTime)
                else:
                    logger.warn("A result seems to already have been processed "
                                "for jobWrapper with batch system ID: %i", jobBatchSystemID)
            else:
                # Process jobs that have gone awry
    
                #In the case that there is nothing happening
                #(no updated jobWrapper to gather for 10 seconds)
                #check if their are any jobs that have run too long
                #(see reissueOverLongJobs) or which
                #have gone missing from the batch system (see reissueMissingJobs)
                if (time.time() - timeSinceJobsLastRescued >=
                    self.config.rescueJobsFrequency): #We only
                    #rescue jobs every N seconds, and when we have
                    #apparently exhausted the current jobWrapper supply
                    self.reissueOverLongJobs()
                    logger.info("Reissued any over long jobs")
    
                    hasNoMissingJobs = self.reissueMissingJobs()
                    if hasNoMissingJobs:
                        timeSinceJobsLastRescued = time.time()
                    else:
                        timeSinceJobsLastRescued += 60 #This means we'll try again
                        #in a minute, providing things are quiet
                    logger.info("Rescued any (long) missing jobs")
    
            # Check on the associated threads and exit if a failure is detected
            self.statsAndLogging.check()
            self.serviceManager.check()
            # the cluster scaler object will only be instantiated if autoscaling is enabled
            if self.clusterScaler is not None:
                self.clusterScaler.check()
    
        logger.info("Finished the main loop")
    
        # Consistency check the toil state
        assert self.toilState.updatedJobs == set()
        assert self.toilState.successorCounts == {}
        assert self.toilState.successorJobStoreIDToPredecessorJobs == {}
        assert self.toilState.serviceJobStoreIDToPredecessorJob == {}
        assert self.toilState.servicesIssued == {}
        # assert toilState.jobsToBeScheduledWithMultiplePredecessors # These are not properly emptied yet
        # assert toilState.hasFailedSuccessors == set() # These are not properly emptied yet

    def issueJob(self, jobStoreID, memory, cores, disk, preemptable):
        """
        Add a job to the queue of jobs
        """
        self.jobsIssued += 1
        if preemptable:
            self._preemptableJobsIssued += 1
        jobCommand = ' '.join((resolveEntryPoint('_toil_worker'), self.jobStoreLocator, jobStoreID))
        jobBatchSystemID = self.batchSystem.issueBatchJob(jobCommand, memory, cores, disk, preemptable)
        self.jobBatchSystemIDToIssuedJob[jobBatchSystemID] = IssuedJob(jobStoreID, memory, cores, disk, preemptable)
        logger.debug("Issued job with job store ID: %s and job batch system ID: "
                     "%s and cores: %.2f, disk: %.2f, and memory: %.2f",
                     jobStoreID, str(jobBatchSystemID), cores, disk, memory)

    def issueJobs(self, jobs):
        """
        Add a list of jobs, each represented as a tuple of (jobStoreID, *resources).
        """
        for jobStoreID, memory, cores, disk, preemptable in jobs:
            self.issueJob(jobStoreID, memory, cores, disk, preemptable)

    def getNumberOfJobsIssued(self, preemptable=None):
        """
        Gets number of jobs that have been added by issueJob(s) and not
        removed by removeJobID

        :param None or boolean preemptable: If none, return all types of jobs.
          If true, return just the number of preemptable jobs. If false, return
          just the number of non-preemptable jobs.
        """
        assert self.jobsIssued >= 0 and self._preemptableJobsIssued >= 0
        if preemptable is None:
            return self.jobsIssued
        elif preemptable:
            return self._preemptableJobsIssued
        else:
            return (self.jobsIssued - self._preemptableJobsIssued)

    def getJob(self, jobBatchSystemID):
        """
        Gets the job file associated the a given id
        """
        return self.jobBatchSystemIDToIssuedJob[jobBatchSystemID].jobStoreID

    def hasJob(self, jobBatchSystemID):
        """
        Returns true if the jobBatchSystemID is in the list of jobs.
        """
        return self.jobBatchSystemIDToIssuedJob.has_key(jobBatchSystemID)

    def getJobIDs(self):
        """
        Gets the set of jobs currently issued.
        """
        return self.jobBatchSystemIDToIssuedJob.keys()

    def removeJobID(self, jobBatchSystemID):
        """
        Removes a job from the batch system.
        """
        assert jobBatchSystemID in self.jobBatchSystemIDToIssuedJob
        self.jobsIssued -= 1
        if self.jobBatchSystemIDToIssuedJob[jobBatchSystemID].preemptable:
            assert self._preemptableJobsIssued > 0
            self._preemptableJobsIssued -= 1
        jobStoreID = self.jobBatchSystemIDToIssuedJob.pop(jobBatchSystemID).jobStoreID
        return jobStoreID

    def killJobs(self, jobsToKill):
        """
        Kills the given set of jobs and then sends them for processing
        """
        if len(jobsToKill) > 0:
            self.batchSystem.killBatchJobs(jobsToKill)
            for jobBatchSystemID in jobsToKill:
                self.processFinishedJob(jobBatchSystemID, 1)

    #Following functions handle error cases for when jobs have gone awry with the batch system.

    def reissueOverLongJobs(self):
        """
        Check each issued job - if it is running for longer than desirable
        issue a kill instruction.
        Wait for the job to die then we pass the job to processFinishedJob.
        """
        maxJobDuration = self.config.maxJobDuration
        jobsToKill = []
        if maxJobDuration < 10000000:  # We won't bother doing anything if the rescue
            # time is more than 16 weeks.
            runningJobs = self.batchSystem.getRunningBatchJobIDs()
            for jobBatchSystemID in runningJobs.keys():
                if runningJobs[jobBatchSystemID] > maxJobDuration:
                    logger.warn("The job: %s has been running for: %s seconds, more than the "
                                "max job duration: %s, we'll kill it",
                                str(self.getJob(jobBatchSystemID)),
                                str(runningJobs[jobBatchSystemID]),
                                str(maxJobDuration))
                    jobsToKill.append(jobBatchSystemID)
            self.killJobs(jobsToKill)

    def reissueMissingJobs(self, killAfterNTimesMissing=3):
        """
        Check all the current job ids are in the list of currently running batch system jobs.
        If a job is missing, we mark it as so, if it is missing for a number of runs of
        this function (say 10).. then we try deleting the job (though its probably lost), we wait
        then we pass the job to processFinishedJob.
        """
        runningJobs = set(self.batchSystem.getIssuedBatchJobIDs())
        jobBatchSystemIDsSet = set(self.getJobIDs())
        #Clean up the reissueMissingJobs_missingHash hash, getting rid of jobs that have turned up
        missingJobIDsSet = set(self.reissueMissingJobs_missingHash.keys())
        for jobBatchSystemID in missingJobIDsSet.difference(jobBatchSystemIDsSet):
            self.reissueMissingJobs_missingHash.pop(jobBatchSystemID)
            logger.warn("Batch system id: %s is no longer missing", str(jobBatchSystemID))
        assert runningJobs.issubset(jobBatchSystemIDsSet) #Assert checks we have
        #no unexpected jobs running
        jobsToKill = []
        for jobBatchSystemID in set(jobBatchSystemIDsSet.difference(runningJobs)):
            jobStoreID = self.getJob(jobBatchSystemID)
            if self.reissueMissingJobs_missingHash.has_key(jobBatchSystemID):
                self.reissueMissingJobs_missingHash[jobBatchSystemID] += 1
            else:
                self.reissueMissingJobs_missingHash[jobBatchSystemID] = 1
            timesMissing = self.reissueMissingJobs_missingHash[jobBatchSystemID]
            logger.warn("Job store ID %s with batch system id %s is missing for the %i time",
                        jobStoreID, str(jobBatchSystemID), timesMissing)
            if timesMissing == killAfterNTimesMissing:
                self.reissueMissingJobs_missingHash.pop(jobBatchSystemID)
                jobsToKill.append(jobBatchSystemID)
        self.killJobs(jobsToKill)
        return len( self.reissueMissingJobs_missingHash ) == 0 #We use this to inform
        #if there are missing jobs

    def processFinishedJob(self, jobBatchSystemID, resultStatus, wallTime=None):
        """
        Function reads a processed jobWrapper file and updates it state.
        """
        def processRemovedJob(jobStoreID):
            if resultStatus != 0:
                logger.warn("Despite the batch system claiming failure the "
                            "jobWrapper %s seems to have finished and been removed", jobStoreID)
            self._updatePredecessorStatus(jobStoreID)

        if wallTime is not None and self.clusterScaler is not None:
            issuedJob = self.jobBatchSystemIDToIssuedJob[jobBatchSystemID]
            self.clusterScaler.addCompletedJob(issuedJob, wallTime)
        jobStoreID = self.removeJobID(jobBatchSystemID)
        if self.jobStore.exists(jobStoreID):
            logger.debug("Job %s continues to exist (i.e. has more to do)" % jobStoreID)
            try:
                jobWrapper = self.jobStore.load(jobStoreID)
            except NoSuchJobException:
                # Avoid importing AWSJobStore as the corresponding extra might be missing
                if self.jobStore.__class__.__name__ == 'AWSJobStore':
                    # We have a ghost job - the job has been deleted but a stale read from
                    # SDB gave us a false positive when we checked for its existence.
                    # Process the job from here as any other job removed from the job store.
                    # This is a temporary work around until https://github.com/BD2KGenomics/toil/issues/1091
                    # is completed
                    logger.warn('Got a stale read from SDB for job %s', jobStoreID)
                    processRemovedJob(jobStoreID)
                    return
                else:
                    raise
            if jobWrapper.logJobStoreFileID is not None:
                with jobWrapper.getLogFileHandle( self.jobStore ) as logFileStream:
                    # more memory efficient than read().striplines() while leaving off the
                    # trailing \n left when using readlines()
                    # http://stackoverflow.com/a/15233739
                    messages = (line.rstrip('\n') for line in logFileStream)
                    logFormat = '\n%s    ' % jobStoreID
                    logger.warn('The job seems to have left a log file, indicating failure: %s\n%s',
                                jobStoreID, logFormat.join(messages))
            if resultStatus != 0:
                # If the batch system returned a non-zero exit code then the worker
                # is assumed not to have captured the failure of the job, so we
                # reduce the retry count here.
                if jobWrapper.logJobStoreFileID is None:
                    logger.warn("No log file is present, despite jobWrapper failing: %s", jobStoreID)
                jobWrapper.setupJobAfterFailure(self.config)
                self.jobStore.update(jobWrapper)
            elif jobStoreID in self.toilState.hasFailedSuccessors:
                # If the job has completed okay, we can remove it from the list of jobs with failed successors
                self.toilState.hasFailedSuccessors.remove(jobStoreID)

            self.toilState.updatedJobs.add((jobWrapper, resultStatus)) #Now we know the
            #jobWrapper is done we can add it to the list of updated jobWrapper files
            logger.debug("Added jobWrapper: %s to active jobs", jobStoreID)
        else:  #The jobWrapper is done
            processRemovedJob(jobStoreID)
    
    @staticmethod    
    def getSuccessors(jobWrapper, alreadySeenSuccessors, jobStore):
        """
        Gets successors of the given job (jobWrapper) by walking the job graph from the job
        through all its successors recursively. 
        Any successor in alreadySeenSuccessors is ignored and not traversed.
        Returns the set of found successors. This set is added to alreadySeenSuccessors.
        """
        successors = set()
        
        def successorRecursion(jobWrapper):
            # For lists of successors
            for successorList in jobWrapper.stack:
                
                # For each successor in list of successors
                for successorJobStoreTuple in successorList:
                    
                    # Id of the successor
                    successorJobStoreID = successorJobStoreTuple[0]
                    
                    # If successor not already visited
                    if successorJobStoreID not in alreadySeenSuccessors:
                        
                        # Add to set of successors
                        successors.add(successorJobStoreID)
                        alreadySeenSuccessors.add(successorJobStoreID)
                        
                        # Recurse if job exists
                        # (job may not exist if already completed)
                        if jobStore.exists(successorJobStoreID):
                            successorRecursion(jobStore.load(successorJobStoreID))
    
        successorRecursion(jobWrapper) # Recurse from jobWrapper
        
        return successors   

    def processTotallyFailedJob(self, jobWrapper):
        """
        Processes a totally failed job.
        """
        # Mark job as a totally failed job
        self.toilState.totalFailedJobs.add(jobWrapper.jobStoreID)

        if jobWrapper.jobStoreID in self.toilState.serviceJobStoreIDToPredecessorJob: # Is
            # a service job
            logger.debug("Service job is being processed as a totally failed job: %s" % jobWrapper.jobStoreID)

            predecessorJobWrapper = self.toilState.serviceJobStoreIDToPredecessorJob[jobWrapper.jobStoreID]

            # This removes the service job as a service of the predecessor
            # and potentially makes the predecessor active
            self._updatePredecessorStatus(jobWrapper.jobStoreID)

            # Remove the start flag, if it still exists. This indicates
            # to the service manager that the job has "started", this prevents
            # the service manager from deadlocking while waiting
            self.jobStore.deleteFile(jobWrapper.startJobStoreID)

            # Signal to any other services in the group that they should
            # terminate. We do this to prevent other services in the set
            # of services from deadlocking waiting for this service to start properly
            if predecessorJobWrapper.jobStoreID in self.toilState.servicesIssued:
                self.serviceManager.killServices(self.toilState.servicesIssued[predecessorJobWrapper.jobStoreID], error=True)
                logger.debug("Job: %s is instructing all the services of its parent job to quit", jobWrapper.jobStoreID)

            self.toilState.hasFailedSuccessors.add(predecessorJobWrapper.jobStoreID) # This ensures that the
            # job will not attempt to run any of it's successors on the stack
        else:
            # Is a non-service job
            assert jobWrapper.jobStoreID not in self.toilState.servicesIssued
            
            # Traverse failed job's successor graph and get the jobStoreID of new successors.
            # Any successor already in toilState.failedSuccessors will not be traversed
            # All successors traversed will be added to toilState.failedSuccessors and returned
            # as a set (unseenSuccessors).
            unseenSuccessors = self.getSuccessors(jobWrapper, self.toilState.failedSuccessors, self.jobStore)
            logger.debug("Found new failed successors: %s of job: %s" % (" ".join(unseenSuccessors), jobWrapper.jobStoreID))
            
            # For each newly found successor
            for successorJobStoreID in unseenSuccessors:
                
                # If the successor is a successor of other jobs that have already tried to schedule it
                if successorJobStoreID in self.toilState.successorJobStoreIDToPredecessorJobs:
                    
                    # For each such predecessor job
                    # (we remove the successor from toilState.successorJobStoreIDToPredecessorJobs to avoid doing 
                    # this multiple times for each failed predecessor)
                    for predecessorJob in self.toilState.successorJobStoreIDToPredecessorJobs.pop(successorJobStoreID):
                        
                        # Reduce the predecessor job's successor count.
                        self.toilState.successorCounts[predecessorJob.jobStoreID] -= 1
                        
                        # Indicate that it has failed jobs.  
                        self.toilState.hasFailedSuccessors.add(predecessorJob.jobStoreID)
                        logger.debug("Marking job: %s as having failed successors (found by reading successors failed job)" % predecessorJob.jobStoreID)
                        
                        # If the predecessor has no remaining successors, add to list of active jobs
                        assert self.toilState.successorCounts[predecessorJob.jobStoreID] >= 0
                        if self.toilState.successorCounts[predecessorJob.jobStoreID] == 0:
                            self.toilState.updatedJobs.add((predecessorJob, 0))
                            
                            # Remove the predecessor job from the set of jobs with successors. 
                            self.toilState.successorCounts.pop(predecessorJob.jobStoreID) 

            # If the job has predecessor(s)
            if jobWrapper.jobStoreID in self.toilState.successorJobStoreIDToPredecessorJobs:
                
                # For each predecessor of the job
                for predecessorJobWrapper in self.toilState.successorJobStoreIDToPredecessorJobs[jobWrapper.jobStoreID]:
                    
                    # Mark the predecessor as failed
                    self.toilState.hasFailedSuccessors.add(predecessorJobWrapper.jobStoreID)
                    logger.debug("Totally failed job: %s is marking direct predecessor: %s as having failed jobs", jobWrapper.jobStoreID, predecessorJobWrapper.jobStoreID)

                self._updatePredecessorStatus(jobWrapper.jobStoreID)

    def _updatePredecessorStatus(self, jobStoreID):
        """
        Update status of predecessors for finished successor job.
        """
        if jobStoreID in self.toilState.serviceJobStoreIDToPredecessorJob:
            # Is a service job
            predecessorJob = self.toilState.serviceJobStoreIDToPredecessorJob.pop(jobStoreID)
            self.toilState.servicesIssued[predecessorJob.jobStoreID].pop(jobStoreID)
            if len(self.toilState.servicesIssued[predecessorJob.jobStoreID]) == 0: # Predecessor job has
                # all its services terminated
                self.toilState.servicesIssued.pop(predecessorJob.jobStoreID) # The job has no running services
                self.toilState.updatedJobs.add((predecessorJob, 0)) # Now we know
                # the job is done we can add it to the list of updated job files
                logger.debug("Job %s services have completed or totally failed, adding to updated jobs" % predecessorJob.jobStoreID)

        elif jobStoreID not in self.toilState.successorJobStoreIDToPredecessorJobs:
            #We have reach the root job
            assert len(self.toilState.updatedJobs) == 0
            assert len(self.toilState.successorJobStoreIDToPredecessorJobs) == 0
            assert len(self.toilState.successorCounts) == 0
            logger.debug("Reached root job %s so no predecessors to clean up" % jobStoreID)

        else:
            # Is a non-root, non-service job
            logger.debug("Cleaning the predecessors of %s" % jobStoreID)
            
            # For each predecessor
            for predecessorJob in self.toilState.successorJobStoreIDToPredecessorJobs.pop(jobStoreID):
                
                # Reduce the predecessor's number of successors by one to indicate the 
                # completion of the jobStoreID job
                self.toilState.successorCounts[predecessorJob.jobStoreID] -= 1

                # If the predecessor job is done and all the successors are complete 
                if self.toilState.successorCounts[predecessorJob.jobStoreID] == 0:
                    
                    # Remove it from the set of jobs with active successors
                    self.toilState.successorCounts.pop(predecessorJob.jobStoreID)

                    # Pop stack at this point, as we can get rid of its successors
                    predecessorJob.stack.pop()
                    
                    # Now we know the job is done we can add it to the list of updated job files
                    assert predecessorJob not in self.toilState.updatedJobs
                    self.toilState.updatedJobs.add((predecessorJob, 0))
                    
                    logger.debug('Job %s has all its non-service successors completed or totally '
                                 'failed', predecessorJob.jobStoreID)
