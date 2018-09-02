import os
import itertools
import subprocess
from dbApp.models import symportal_framework, data_set, reference_sequence, data_set_sample_sequence, analysis_type, analysis_group, data_set_sample, data_analysis, clade_collection, clade_collection_type
from multiprocessing import Queue, Process, Manager
from django import db
import pickle
import csv
import numpy as np
from collections import defaultdict
import shutil
import re
import json
import glob
from datetime import datetime
import sys
import pandas as pd
from output import div_output_pre_analysis_new_meta_and_new_dss_structure
from general import *
from distance import generate_within_clade_UniFrac_distances_samples
from plotting import generate_stacked_bar_data_submission, plot_between_sample_distance_scatter


def logQCErrorAndContinue(datasetsampleinstanceinq, samplename, errorreason):
    print('Error in processing sample: {}'.format(samplename))
    datasetsampleinstanceinq.finalUniqueSeqNum = 0
    datasetsampleinstanceinq.finalTotSeqNum = 0
    datasetsampleinstanceinq.initialProcessingComplete = True
    datasetsampleinstanceinq.errorInProcessing = True
    datasetsampleinstanceinq.errorReason = errorreason
    datasetsampleinstanceinq.save()

    return

def worker(input, output, wkd, dataSubID, e_val_collection_dict, reference_db_name):
    '''This worker performs the pre-MED processing'''
    dataSubInQ = data_set.objects.get(id=dataSubID)
    for contigPair in iter(input.get, 'STOP'):
        sampleName = contigPair.split('\t')[0].replace('[dS]','-')

        dataSetSampleInstanceInQ = data_set_sample.objects.get(name=sampleName, dataSubmissionFrom=dataSubInQ)
        # Only process samples that have not already had this done.
        # This should be handy in the event of crashed midprocessing
        if dataSetSampleInstanceInQ.initialProcessingComplete == False:
            ###### NB We will always crop with the SYMVAR primers as they produce the shortest product
            primerFwdSeq = 'GAATTGCAGAACTCCGTGAACC'  # Written 5'-->3'
            primerRevSeq = 'CGGGTTCWCTTGTYTGACTTCATGC'  # Written 5'-->3'

            oligoFile = [
                r'#SYM_VAR_5.8S2',
                'forward\t{0}'.format(primerFwdSeq),
                r'#SYM_VAR_REV',
                'reverse\t{0}'.format(primerRevSeq)
            ]


            #Initial Mothur QC, making contigs, screening for ambiguous calls and homopolymers
            # Uniqueing, discarding <2 abundance seqs, removing primers and adapters

            sys.stdout.write('{0}: QC started\n'.format(sampleName))
            currentDir = r'{0}/{1}/'.format(wkd, sampleName)
            os.makedirs(currentDir, exist_ok=True)
            stabilityFile = [contigPair]
            stabilityFileName = r'{0}{1}'.format(sampleName,'stability.files')
            rootName = r'{0}stability'.format(sampleName)
            stabilityFilePath = r'{0}{1}'.format(currentDir,stabilityFileName)
            writeListToDestination(stabilityFilePath, stabilityFile)
            # Write oligos file to directory
            writeListToDestination('{0}{1}'.format(currentDir, 'primers.oligos'), oligoFile)
            # NB mothur is working very strangely with the python subprocess command. For some
            # reason it is adding in an extra 'mothur' before the filename in the input directory
            # reason it is adding in an extra 'mothur' before the filename in the input directory
            # As such we will have to enter all of the paths to files absolutely

            # I am going to have to implement a check here that looks to see if the sequences are reverse complement or not.
            # Mothur pcr.seqs does not check to see if this is a problem. You simply get all of your seqs thrown out


            mBatchFile = [
                r'set.dir(input={0})'.format(currentDir),
                r'set.dir(output={0})'.format(currentDir),
                r'make.contigs(file={}{})'.format(currentDir, stabilityFileName),
                r'summary.seqs(fasta={}{}.trim.contigs.fasta)'.format(currentDir, rootName),
                r'screen.seqs(fasta={0}{1}.trim.contigs.fasta, group={0}{1}.contigs.groups, maxambig=0, maxhomop=5)'.format(
                    currentDir, rootName),
                r'summary.seqs(fasta={0}{1}.trim.contigs.good.fasta)'.format(currentDir, rootName),
                r'unique.seqs(fasta={0}{1}.trim.contigs.good.fasta)'.format(currentDir, rootName),
                r'summary.seqs(fasta={0}{1}.trim.contigs.good.unique.fasta, name={0}{1}.trim.contigs.good.names)'.format(
                    currentDir, rootName),
                r'split.abund(cutoff=2, fasta={0}{1}.trim.contigs.good.unique.fasta, name={0}{1}.trim.contigs.good.names, group={0}{1}.contigs.good.groups)'.format(
                    currentDir, rootName),
                r'summary.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.fasta, name={0}{1}.trim.contigs.good.abund.names)'.format(
                    currentDir, rootName),
                r'summary.seqs(fasta={0}{1}.trim.contigs.good.unique.rare.fasta, name={0}{1}.trim.contigs.good.rare.names)'.format(
                    currentDir, rootName),
                r'pcr.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.fasta, group={0}{1}.contigs.good.abund.groups, name={0}{1}.trim.contigs.good.abund.names, oligos={0}primers.oligos, pdiffs=2, rdiffs=2)'.format(
                    currentDir, rootName)
            ]

            mBatchFilePath = r'{0}{1}{2}'.format(currentDir, 'mBatchFile', sampleName)
            writeListToDestination(mBatchFilePath, mBatchFile)


            error = False

            with subprocess.Popen(['mothur', '{0}'.format(mBatchFilePath)], stdout=subprocess.PIPE, bufsize=1, universal_newlines=True) as p:
                for line in p.stdout:
                    # print(line)
                    if '[WARNING]: Blank fasta name, ignoring read.' in line:



                        p.terminate()
                        errorReason = 'Blank fasta name'
                        logQCErrorAndContinue(dataSetSampleInstanceInQ, sampleName, errorReason)
                        error = True
                        output.put(sampleName)
                        break


            if error:
                continue


            # Here check the outputted files to see if they are reverse complement or not by running the pcr.seqs and checking the results

            # Check to see if there are sequences in the PCR output file
            lastSummary = readDefinedFileToList(
                '{}{}.trim.contigs.good.unique.abund.pcr.fasta'.format(currentDir, rootName))
            if len(lastSummary) == 0:  # If this file is empty
                # Then these sequences may well be reverse complement so we need to try to rev first
                mBatchRev = [
                    r'reverse.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.fasta)'.format(currentDir, rootName),
                    r'pcr.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.rc.fasta, group={0}{1}.contigs.good.abund.groups, name={0}{1}.trim.contigs.good.abund.names, oligos={0}primers.oligos, pdiffs=2, rdiffs=2)'.format(
                        currentDir, rootName)
                ]
                mBatchFilePath = r'{0}{1}{2}'.format(currentDir, 'mBatchFile', sampleName)
                writeListToDestination(mBatchFilePath, mBatchRev)
                completedProcess = subprocess.run(
                    ['mothur', r'{0}'.format(mBatchFilePath)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                # At this point the sequences will be reversed and they will have been renamed so we
                # can just change the name of the .rc file to the orignal .fasta file that we inputted with
                # This way we don't need to change the rest of the mothur pipe.
                subprocess.run([r'mv', r'{0}{1}.trim.contigs.good.unique.abund.rc.pcr.fasta'.format(currentDir,rootName), r'{0}{1}.trim.contigs.good.unique.abund.pcr.fasta'.format(currentDir,rootName)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            lastSummary = readDefinedFileToList(
                '{}{}.trim.contigs.good.unique.abund.pcr.fasta'.format(currentDir, rootName))
            if len(lastSummary) == 0:  # If this file is still empty, then the problem was not solved by reverse complementing


                errorReason = 'error in inital QC'
                logQCErrorAndContinue(dataSetSampleInstanceInQ, sampleName, errorReason)
                output.put(sampleName)
                continue


            mBatchFileContinued = [
                r'summary.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.pcr.fasta, name={0}{1}.trim.contigs.good.abund.pcr.names)'.format(
                    currentDir, rootName),
                r'unique.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.pcr.fasta, name={0}{1}.trim.contigs.good.abund.pcr.names)'.format(
                    currentDir, rootName),
                r'summary.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.pcr.unique.fasta, name={0}{1}.trim.contigs.good.unique.abund.pcr.names)'.format(
                    currentDir, rootName)
            ]

            mBatchFilePath = r'{0}{1}{2}'.format(currentDir, 'mBatchFile', sampleName)
            writeListToDestination(mBatchFilePath, mBatchFileContinued)
            completedProcess = subprocess.run(
                ['mothur', r'{0}'.format(mBatchFilePath)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)


            if completedProcess.returncode == 1:
                errorReason = 'error in inital QC'
                logQCErrorAndContinue(dataSetSampleInstanceInQ, sampleName, errorReason)
                output.put(sampleName)
                continue


            # Check to see if there are sequences in the PCR output file
            try:
                lastSummary = readDefinedFileToList(
                    '{}{}.trim.contigs.good.unique.abund.pcr.unique.fasta'.format(currentDir, rootName))
                if len(lastSummary) == 0:  # If this file is empty
                    errorReason = 'error in inital QC'
                    logQCErrorAndContinue(dataSetSampleInstanceInQ, sampleName, errorReason)
                    output.put(sampleName)
                    continue
            except: # If there is no file then we can assume sample has a problem
                logQCErrorAndContinue(dataSetSampleInstanceInQ, sampleName)
                continue


            # Get number of sequences after make.contig
            lastSummary = readDefinedFileToList('{}{}.trim.contigs.summary'.format(currentDir, rootName))
            number_of_seqs_contig_absolute = len(lastSummary) - 1
            dataSetSampleInstanceInQ.initialTotSeqNum = number_of_seqs_contig_absolute
            sys.stdout.write('{}: dataSetSampleInstanceInQ.initialTotSeqNum = {}\n'.format(sampleName, number_of_seqs_contig_absolute))

            # Get number of sequences after unique
            lastSummary = readDefinedFileToList('{}{}.trim.contigs.good.unique.abund.pcr.unique.summary'.format(currentDir, rootName))
            number_of_seqs_contig_unique = len(lastSummary) - 1
            dataSetSampleInstanceInQ.initialUniqueSeqNum = number_of_seqs_contig_unique
            sys.stdout.write('{}: dataSetSampleInstanceInQ.initialUniqueSeqNum = {}\n'.format(sampleName, number_of_seqs_contig_unique))

            # Get absolute number of sequences after after sequence QC
            last_summary = readDefinedFileToList('{}{}.trim.contigs.good.unique.abund.pcr.unique.summary'.format(currentDir, rootName))
            absolute_count = 0
            for line in last_summary[1:]:
                absolute_count += int(line.split('\t')[6])
            dataSetSampleInstanceInQ.post_seq_qc_absolute_num_seqs = absolute_count
            dataSetSampleInstanceInQ.save()
            sys.stdout.write('{}: dataSetSampleInstanceInQ.post_seq_qc_absolute_num_seqs = {}\n'.format(sampleName,
                                                                                                   absolute_count))

            sys.stdout.write('{}: Initial mothur complete\n'.format(sampleName))
            # Each sampleDataDir should contain a set of .fasta, .name and .group files that we can use to do local blasts with

            ncbircFile = []
            db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'symbiodiniumDB'))
            ncbircFile.extend(["[BLAST]", "BLASTDB={}".format(db_path)])





            # Run local blast of all seqs and determine clade. Discard seqs below evalue cutoff and write out new fasta, name, group and clade dict
            sys.stdout.write('{}: verifying seqs are Symbiodinium and determining clade\n'.format(sampleName))

            #write the .ncbirc file that gives the location of the db
            writeListToDestination("{0}.ncbirc".format(currentDir), ncbircFile)

            #Read in the fasta, name and group files and convert to dics
            fastaFile = readDefinedFileToList('{0}{1}.trim.contigs.good.unique.abund.pcr.unique.fasta'.format(currentDir, rootName))
            uniqueFastaFile = createNoSpaceFastaFile(fastaFile)
            writeListToDestination('{}blastInputFasta.fa'.format(currentDir), uniqueFastaFile)
            fastaDict = createDictFromFasta(uniqueFastaFile)
            nameFile = readDefinedFileToList('{0}{1}.trim.contigs.good.unique.abund.pcr.names'.format(currentDir, rootName))
            nameDict = {a.split('\t')[0]: a for a in nameFile}

            groupFile = readDefinedFileToList('{0}{1}.contigs.good.abund.pcr.groups'.format(currentDir, rootName))

            # Set up environment for running local blast

            blastOutputPath = r'{}blast.out'.format(currentDir)
            outputFmt = "6 qseqid sseqid staxids evalue"
            inputPath = r'{}blastInputFasta.fa'.format(currentDir)
            os.chdir(currentDir)

            # Run local blast
            # completedProcess = subprocess.run([blastnPath, '-out', blastOutputPath, '-outfmt', outputFmt, '-query', inputPath, '-db', 'symbiodinium.fa', '-max_target_seqs', '1', '-num_threads', '1'])
            completedProcess = subprocess.run(['blastn', '-out', blastOutputPath, '-outfmt', outputFmt, '-query', inputPath, '-db', reference_db_name,
                 '-max_target_seqs', '1', '-num_threads', '1'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            sys.stdout.write('{}: BLAST complete\n'.format(sampleName))
            # Read in blast output
            blastOutputFile = readDefinedFileToList(r'{}blast.out'.format(currentDir))
            blastDict = {a.split('\t')[0]: a.split('\t')[1][-1] for a in blastOutputFile}
            throwAwaySeqs = []
            ###### Uncomment for blasting QC
            #######blastedSeqs = []
            #####NB it turns out that the blast will not always return a match.
            # If a match is not returned it means the sequence did not have a significant match to the seqs in the db
            #Add any seqs that did not return a blast match to the throwAwaySeq list
            diff = set(fastaDict.keys()) - set(blastDict.keys())
            throwAwaySeqs.extend(list(diff))
            sys.stdout.write(
                '{}: {} sequences thrown out initially due to being too divergent from reference sequences\n'.format(
                    sampleName, len(list(diff))))
            ## 030518 We are starting to throw away Symbiodinium sequences here, especially in the non-coral samples
            # I think we will need to severely relax the e value cut off in order to incorporate more sequences

            # NB note that the blast results sometime return several matches for the same seq.
            # as such we will use the already_processed_blast_seq_resulst to make sure that we only
            # process each sequence once.
            already_processed_blast_seq_result = []
            for line in blastOutputFile:
                seqInQ = line.split('\t')[0]
                if seqInQ in already_processed_blast_seq_result:
                    continue
                already_processed_blast_seq_result.append(seqInQ)
                try:
                    evaluePower = int(line.split('\t')[3].split('-')[1])
                    if evaluePower < 100:  # evalue cut off, collect sequences that don't make the cut
                        throwAwaySeqs.append(seqInQ)
                        # incorporate the size cutoff here that would normally happen below
                        if 184 < len(fastaDict[seqInQ]) < 310:
                            if fastaDict[seqInQ] in e_val_collection_dict.keys():
                                e_val_collection_dict[fastaDict[seqInQ]] += 1
                            else:
                                e_val_collection_dict[fastaDict[seqInQ]] = 1
                except:
                    throwAwaySeqs.append(seqInQ)
                    if 184 < len(fastaDict[seqInQ]) < 310:
                        if fastaDict[seqInQ] in e_val_collection_dict.keys():
                            e_val_collection_dict[fastaDict[seqInQ]] += 1
                        else:
                            e_val_collection_dict[fastaDict[seqInQ]] = 1

            #have a look at the above code and see how we can get a total count for all of the sequences
            # that were thrown away. I think we'll likely have to look for a name file to be able to look
            # the thrown away sequences up in.
            # Add number of absolute sesquences that are not Symbiodinium

            # NB it turns out that sometimes a sequence is returned in the blast results twice! This was messing up
            # our meta-analysis reporting. This will be fixed by working with sets of the throwaway sequences
            temp_count = 0
            for seq_name in list(set(throwAwaySeqs)):
                temp_count += len(nameDict[seq_name].split('\t')[1].split(','))
            dataSetSampleInstanceInQ.non_sym_absolute_num_seqs = temp_count
            # Add details of non-symbiodinium unique seqs
            dataSetSampleInstanceInQ.nonSymSeqsNum = len(set(throwAwaySeqs))
            dataSetSampleInstanceInQ.save()

            ###### Uncomment for blasting QC
            ########notBlasted = list(set(blastedSeqs)-set(nameDict.keys()))
            ########print('notblasted: ' + str(len(notBlasted)))

            # Output new fasta, name and group files that don't contain seqs that didn't make the cut

            sys.stdout.write('{}: discarding {} unique sequences for evalue cutoff violations\n'.format(sampleName, str(len(throwAwaySeqs))))
            newFasta = []
            newName = []
            newGroup = []
            cladalDict = {}
            count = 0
            listOfBadSeqs = []
            for line in groupFile:
                sequence = line.split('\t')[0]
                if sequence not in throwAwaySeqs:
                    newGroup.append(line)
                    # The fastaDict is only meant to have the unique seqs in so this will go to 'except' a lot. This is OK and normal
                    try:
                        newFasta.extend(['>{}'.format(sequence), fastaDict[sequence]])
                    except:
                        pass

                    try:
                        newName.append(nameDict[sequence])
                        listOfSameSeqNames = nameDict[sequence].split('\t')[1].split(',')
                        clade = blastDict[sequence]

                        for seqName in listOfSameSeqNames:
                            cladalDict[seqName] = clade
                    except:
                        pass
            # Now write the files out
            if not newFasta:
                # Then the fasta is blank and we have got no good Symbiodinium seqs
                errorReason = 'No Symbiodinium sequences left after blast annotation'
                logQCErrorAndContinue(dataSetSampleInstanceInQ, sampleName, errorReason)
                output.put(sampleName)
                continue
            sys.stdout.write('{}: non-Symbiodinium sequences binned\n'.format(sampleName))
            writeListToDestination('{0}{1}.trim.contigs.good.unique.abund.pcr.blast.fasta'.format(currentDir, rootName), newFasta)
            writeListToDestination('{0}{1}.trim.contigs.good.abund.pcr.blast.names'.format(currentDir, rootName), newName)
            writeListToDestination('{0}{1}.contigs.good.abund.pcr.blast.groups'.format(currentDir, rootName), newGroup)
            writeByteObjectToDefinedDirectory('{0}{1}.cladeDict.dict'.format(currentDir, rootName), cladalDict)
            # At this point we have the newFasta, newName, newGroup. These all only contain sequences that were
            # above the blast evalue cutoff.
            # We also have the cladalDict for all of these sequences.summary

            # Now finish off the mothur analyses by discarding by size range
            # Have to find how big the average seq was and then discard 50 bigger or smaller than this
            # Read in last summary file

            # I am now going to switch this to an absolute size range as I am having problems with Mani's sequences.
            # For some reason he is having an extraordinarily high number of very short sequence (i.e. 15bp long).
            # These are not being thrown out in the blast work. As such the average is being thrown off. and means that our
            # upper size limit is only about 200.
            # I have calculated the averages of each of the clades for our reference sequences so far
            '''Clade A 234.09815950920245
                Clade B 266.79896907216494
                Clade C 261.86832986832985
                Clade D 260.44158075601376
                 '''
            # I will take our absolute cutoffs from these numbers (+- 50 bp) so 184-310
            secondmBatchFilePathList = []
            lastSummary = readDefinedFileToList('{0}{1}.trim.contigs.good.unique.abund.pcr.unique.summary'.format(currentDir, rootName))
            sum = 0
            for line in lastSummary[1:]:
                sum += int(line.split('\t')[3])
            average = int(sum/len(lastSummary))
            cutOffLower = 184
            cutOffUpper = 310


            secondmBatchFile = [
                r'set.dir(input={0})'.format(currentDir),
                r'set.dir(output={0})'.format(currentDir),
                r'screen.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.pcr.blast.fasta, name={0}{1}.trim.contigs.good.abund.pcr.blast.names, group={0}{1}.contigs.good.abund.pcr.blast.groups,  minlength={2}, maxlength={3})'.format(currentDir,rootName,cutOffLower,cutOffUpper),
                r'summary.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.pcr.blast.good.fasta, name={0}{1}.trim.contigs.good.abund.pcr.blast.good.names)'.format(currentDir,rootName),
                r'unique.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.pcr.blast.good.fasta, name={0}{1}.trim.contigs.good.abund.pcr.blast.good.names)'.format(
                    currentDir, rootName),
                r'summary.seqs(fasta={0}{1}.trim.contigs.good.unique.abund.pcr.blast.good.unique.fasta, name={0}{1}.trim.contigs.good.unique.abund.pcr.blast.good.names)'.format(
                    currentDir, rootName),
            ]
            mBatchFilePath = r'{0}{1}{2}'.format(currentDir, 'mBatchFile', rootName)
            secondmBatchFilePathList.append(mBatchFilePath)

            writeListToDestination(mBatchFilePath, secondmBatchFile)
            completedProcess = subprocess.run(['mothur', r'{0}'.format(mBatchFilePath)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            if completedProcess.returncode == 1:
                errorReason = 'No Symbiodinium sequences left after size screening'
                logQCErrorAndContinue(dataSetSampleInstanceInQ, sampleName, errorReason)
                output.put(sampleName)
                continue


            #### Here make cladally separated fastas

            try:
                fastaFile = readDefinedFileToList('{0}{1}.trim.contigs.good.unique.abund.pcr.blast.good.unique.fasta'.format(currentDir, rootName))
                nameFile = readDefinedFileToList('{0}{1}.trim.contigs.good.unique.abund.pcr.blast.good.names'.format(currentDir, rootName))
            except:
                logQCErrorAndContinue(dataSetSampleInstanceInQ, sampleName)
                continue

            sys.stdout.write('{}: final Mothur completed\n'.format(sampleName))
            fastaDict = createDictFromFasta(fastaFile)

            nameDict = {a.split('\t')[0]: a for a in nameFile}
            cladeDict = readByteObjectFromDefinedDirectory('{0}{1}.cladeDict.dict'.format(currentDir, rootName))
            cladeDirs = []
            cladeFastas = {}
            for line in nameFile:
                sequence = line.split('\t')[0]
                clade = cladeDict[sequence]
                if clade in cladeDirs:#Already have a dir for it
                    cladeFastas[clade][0].extend(['>{}'.format(sequence), fastaDict[sequence]])
                    cladeFastas[clade][1].append(nameDict[sequence])

                else: #Make dir and add empty fasta list to cladeFastas
                    cladeFastas[clade] = ([],[])
                    cladeDirs.append(clade)
                    os.makedirs(r'{}{}'.format(currentDir,clade), exist_ok=True)
                    cladeFastas[clade][0].extend(['>{}'.format(sequence), fastaDict[sequence]])
                    cladeFastas[clade][1].append(nameDict[sequence])

            total_debug_absolute = 0
            total_debug_unique = 0
            for someclade in cladeDirs:
                ### Debug###

                # work out the absolute number of sequences and unique sequences from these file and compare to the below
                temp_name = cladeFastas[someclade][1]
                total_debug_unique += len(temp_name)
                for temp_line in temp_name:
                    total_debug_absolute += len(temp_line.split('\t')[1].split(','))
                ####
                writeListToDestination(r'{0}{1}/{2}.QCed.clade{1}.fasta'.format(currentDir,someclade,rootName.replace('stability','')),cladeFastas[someclade][0])
                writeListToDestination(r'{0}{1}/{2}.QCed.clade{1}.names'.format(currentDir, someclade, rootName.replace('stability','')), cladeFastas[someclade][1])

            # Here we have cladaly sorted fasta and name file in new directory

            # now populate the data set sample with the qc meta-data
            # get unique seqs remaining
            dataSetSampleInstanceInQ.finalUniqueSeqNum = len(nameDict)
            #Get total number of sequences
            count = 0
            for nameKey in nameDict.keys():
                count += len(nameDict[nameKey].split('\t')[1].split(','))
            dataSetSampleInstanceInQ.finalTotSeqNum = count
            # now get the seqs lost through size violations through subtraction
            dataSetSampleInstanceInQ.size_violation_absolute = dataSetSampleInstanceInQ.post_seq_qc_absolute_num_seqs - dataSetSampleInstanceInQ.finalTotSeqNum - dataSetSampleInstanceInQ.non_sym_absolute_num_seqs
            dataSetSampleInstanceInQ.size_violation_unique = dataSetSampleInstanceInQ.initialUniqueSeqNum - dataSetSampleInstanceInQ.finalUniqueSeqNum - dataSetSampleInstanceInQ.nonSymSeqsNum

            # Now update the data_set_sample instance to set initialProcessingComplete to True
            dataSetSampleInstanceInQ.initialProcessingComplete = True
            dataSetSampleInstanceInQ.save()
            sys.stdout.write('{}: initial processing complete\n'.format(sampleName))
            sys.stdout.write('{}: dataSetSampleInstanceInQ.finalUniqueSeqNum = {}\n'.format(sampleName, len(nameDict)))
            sys.stdout.write('{}: dataSetSampleInstanceInQ.finalTotSeqNum = {}\n'.format(sampleName, count))

            os.chdir(currentDir)
            fileList = [f for f in os.listdir(currentDir) if f.endswith((".names", ".fasta", ".qual", ".summary", ".oligos",
                                                             ".accnos", ".files", ".groups", ".logfile", ".dict", ".fa",
                                                             ".out"))]
            for f in fileList:
                os.remove(f)

            sys.stdout.write('{}: pre-MED processing completed\n'.format(sampleName))

    return

def perform_MED(wkd, ID, numProc):

    # Create mothur batch for each .fasta .name pair to be deuniqued
    # Put in directory list, run via multiprocessing
    samplesCollection = data_set_sample.objects.filter(dataSubmissionFrom=data_set.objects.get(id=ID))
    mBatchFilePathList = []
    for dataSetSampleInstance in samplesCollection: # For each samples directory
        sampleName = dataSetSampleInstance.name
        fullPath = '{}/{}'.format(wkd, sampleName)

        #http: // stackoverflow.com / questions / 3207219 / how - to - list - all - files - of - a - directory
        listOfDirs = []
        for (dirpath, dirnames, filenames) in os.walk(fullPath):
            listOfDirs.extend(dirnames)
            break
        for directory in listOfDirs:# for each cladal directory
            fastaFilePath = ''
            nameFilePath = ''
            pathToDir = '{0}/{1}'.format(fullPath, directory)
            cladeName = directory
            # For each of the files in each of the Cladal directories
            listOfFiles = []
            for (dirpath, dirnames, filenames) in os.walk(pathToDir):
                listOfFiles.extend(filenames)
                break

            for files in listOfFiles:
                if '.fasta' in files and '.redundant' not in files:
                    fastaFilePath = '{0}/{1}'.format(pathToDir, files)
                elif '.names' in files:
                    nameFilePath = '{0}/{1}'.format(pathToDir, files)

            # Build a quick mBatchFile
            mBatchFile = [
                r'set.dir(input={0}/)'.format(pathToDir),
                r'set.dir(output={0}/)'.format(pathToDir),
                r'deunique.seqs(fasta={0}, name={1})'.format(fastaFilePath, nameFilePath)
            ]
            mBatchFilePath = '{0}/{1}'.format(pathToDir, '{0}.{1}.{2}'.format(sampleName, cladeName, 'mBatchFile'))
            writeListToDestination(mBatchFilePath, mBatchFile)
            mBatchFilePathList.append(mBatchFilePath)

    # Create the queues that will hold the mBatchFile paths
    taskQueue = Queue()
    doneQueue = Queue()

    for mBatchFilePath in mBatchFilePathList:
        taskQueue.put(mBatchFilePath)


    for n in range(numProc):
        taskQueue.put('STOP')

    allProcesses = []

    # http://stackoverflow.com/questions/8242837/django-multiprocessing-and-database-connections
    for n in range(numProc):
        p = Process(target=deuniqueWorker, args=(taskQueue, doneQueue))
        allProcesses.append(p)
        p.start()

    # Collect the list of deuniqued directories to use for MED analyses
    listOfDeuniquedFastaPaths = []
    for i in range(len(mBatchFilePathList)):
        listOfDeuniquedFastaPaths.append(doneQueue.get())

    for p in allProcesses:
        p.join()



    return listOfDeuniquedFastaPaths

def deuniqueWorker(input, output):

    # This currently works through a list of paths to batch files to be uniques.
    # But at each of these locations once the modified deuniqued file has been written we can then perform the MED
    # analysis on the file in each of the directories.
    # We also want to be able to read in the results of the MED but we will not be able to do that as MP so we
    # will have to save the list of directories and go through them one by one to create the sequences

    for mBatchFilePath in iter(input.get, 'STOP'):

        cwd = os.path.dirname(mBatchFilePath)
        sampleName = cwd.split('/')[-2]

        sys.stdout.write('{}: deuniqueing QCed seqs\n'.format(sampleName))
        found = True

        # Run the dunique
        completedProcess = subprocess.run(['mothur', r'{0}'.format(mBatchFilePath)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Modify the deuniqued fasta to append sample name to all of the sequences
        # Get list of files in directory
        deuniquedFasta = []

        # Replace '_' in name as MED uses text up to first underscore as sample name
        # This shouldn't be necessary
        #sampleName = sampleName.replace('_', '-')
        listOfFiles = []
        for (dirpath, dirnames, filenames) in os.walk(cwd):
            listOfFiles.extend(filenames)
            break
        pathToFile = None
        for file in listOfFiles:
            if '.redundant' in file: # Then this is the deuniqued fasta
                pathToFile = '{0}/{1}'.format(cwd, file)

                break
        deuniquedFasta = readDefinedFileToList(pathToFile)
        deuniquedFasta = ['{0}{1}_{2}'.format(a[0],sampleName,a[1:].replace('_','-')) if a[0] == '>' else a for a in deuniquedFasta]
        #write the modified deuniquedFasta to list
        writeListToDestination(pathToFile, deuniquedFasta)
        # Put the path to the deuniqued fasta into the output list for use in MED analyses
        output.put('{}/{}/'.format(os.path.dirname(pathToFile), 'MEDOUT'))


        # The fasta that we want to pad and MED is the 'file'
        sys.stdout.write('{}: padding alignment\n'.format(sampleName))
        completedProcess = subprocess.run([r'o-pad-with-gaps', r'{}'.format(pathToFile)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if completedProcess.returncode == 1:
            pear = 'appe;'
        # Now run MED
        listOfFiles = []
        for (dirpath, dirnames, filenames) in os.walk(cwd):
            listOfFiles.extend(filenames)
            break
        for file in listOfFiles:
            if 'PADDED' in file:
                pathToFile = '{0}/{1}'.format(cwd, file)
                break
        MEDOutDir = '{}/{}/'.format(cwd, 'MEDOUT')
        os.makedirs(MEDOutDir, exist_ok=True)
        sys.stdout.write('{}: running MED\n'.format(sampleName))
        completedProcess = subprocess.run(
            [r'decompose', '--skip-gexf-files', '--skip-gen-figures', '--skip-gen-html', '--skip-check-input', '-o',
             MEDOutDir, pathToFile], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        sys.stdout.write('{}: MED complete\n'.format(sampleName))



def checkIfSeqInQHadRefSeqMatch(seqInQ, nodeName, refSeqIdDict, nodeToRefDict, refSeqIDNameDict):
    # seqInQ = the MED node sequence in question
    # refSeqIdDict = dictionary of all current ref_sequences sequences (KEY) to their ID (VALUE).
    # We use this to look to see if there is an equivalent refSeq Sequence for the sequence in question
    # This take into account whether the seqInQ could be a subset or super set of one of the
    # refSeq.sequences
    # Will return false if no refSeq match is found

    # first check to see if seq is found
    if seqInQ in refSeqIdDict:  # Found actual seq in dict
        # assign the MED node name to the reference_sequence ID that it matches
        nodeToRefDict[nodeName] = refSeqIdDict[seqInQ]
        sys.stdout.write('\rAssigning MED node {} to existing reference sequence {}'.format(nodeName,
                                                                               refSeqIDNameDict[refSeqIdDict[seqInQ]]))
        return True
    elif 'A' + seqInQ in refSeqIdDict:  # This was a seq shorter than refseq but we can associate it to this ref seq
        # assign the MED node name to the reference_sequence ID that it matches
        nodeToRefDict[nodeName] = refSeqIdDict['A' + seqInQ]
        sys.stdout.write('\rAssigning MED node {} to existing reference sequence {}'.format(nodeName, refSeqIDNameDict[
            refSeqIdDict['A' + seqInQ]]))
        return True
    else:  # This checks if either the seq in question is found in the sequence of a reference_sequence
        # or if the seq in question is bigger than a refseq sequence and is a super set of it
        # In either of these cases we should consider this a match and use the refseq matched to.
        # This might be very coputationally expensive but lets give it a go

        for ref_seq_key in refSeqIdDict.keys():
            if seqInQ in ref_seq_key or ref_seq_key in seqInQ:
                # Then this is a match
                nodeToRefDict[nodeName] = refSeqIdDict[ref_seq_key]
                sys.stdout.write('\rAssigning MED node {} to existing reference sequence {}'.format(
                    nodeName, refSeqIDNameDict[refSeqIdDict[ref_seq_key]]))
                return True
    return False

def collateFilesForMed(listofdeuniquedfastapaths, wkd):
    # To avoid a memory crash we append each deuniqued fasta directly to the master fasta on disk
    # This way we don't hold the master fasta in memory as a list
    cladeList = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I']
    # wkd = '/'.join(listofdeuniquedfastapaths[0].split('/')[:5])
    for clade in cladeList:
        createNewFile('{}/deuniquedFastaForMED/redundant.{}.fasta'.format(wkd, clade))
    # list will hold all of the fastas
    #cladeSepFastaList = [[] for a in cladeList]
    for deUFastaPath in listofdeuniquedfastapaths:
        deuniquedFasta = readDefinedFileToList(deUFastaPath)
        clade = deUFastaPath.split('/')[-2]
        writeLinesToFile('{}/deuniquedFastaForMED/redundant.{}.fasta'.format(wkd, clade), deuniquedFasta)

    # Count number of seqs in each file
    # If empty, then delete
    for clade in cladeList:
        fastaPath = '{}/deuniquedFastaForMED/redundant.{}.fasta'.format(wkd, clade)
        if checkIfFileEmpty(fastaPath): # Delete the MED input clade files that are empty
            os.remove(fastaPath)

    return


def create_data_set_sample_sequences_from_MED_nodes(wkd, ID, MEDDirs):
    ''' Here we have modified the original method processMEDDataDirectCCDefinition'''
    # We are going to change this so that we go to each of the MEDDirs, which represent the clades within samples
    # that have had MED analyses run in them and we are going to use the below code to populate sequences to
    # the CCs and samples

    # in checkIfSeqInQHadRefSeqMatch method below we are currently doing lots of database look ups to
    # get the names of reference_sequecnes
    # this is likely quite expensive so I think it will be easier to make a dict for this purpose which is
    # reference_sequence.id (KEY) reference_sequence.name (VALUE)
    reference_sequence_ID_to_name_dict = {refSeq.id: refSeq.name for refSeq in reference_sequence.objects.all()}

    # This is a dict of key = reference_sequence.sequence value = reference_sequence.id for all refseqs
    # currently held in the database
    # We will use this to see if the sequence in question has a match, or is found in (this is key
    # as some of the seqs are one bp smaller than the reference seqs) there reference sequences
    reference_sequence_sequence_to_ID_dict = {refSeq.sequence: refSeq.id for refSeq in reference_sequence.objects.all()}

    cladeList = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I']
    for dir in MEDDirs: # For each of the directories where we did MED
        os.chdir(dir)

        # Get the sample
        sampleName = dir.split('/')[-4]

        # Get the clade
        clade = dir.split('/')[-3]

        sys.stdout.write('\n\nPopulating {} with clade {} sequences\n'.format(sampleName, clade))


        # Read in the node file
        try:
            nodeFile = readDefinedFileToList('NODE-REPRESENTATIVES.fasta')
        except:
            # if no node file found move on to the next directory
            continue
        nodeFile = [line.replace('-', '') if line[0] != '>' else line for line in nodeFile]

        # Create nodeToRefDict that will be populated
        nodeToRefDict = {}

        ############ ASSOCIATE MED NODES TO EXISITING REFSEQS OR CREATE NEW REFSEQS #########
        # Look up seq of each of the MED nodes with reference_sequence table
        # See if the seq in Q matches a reference_sequence, if so, associate
        listOfRefSeqs = []
        for i in range(len(nodeFile)):
            # We were having a problem here where some of the seqs were 1bp shorter than the reference seqs
            # As such they werent matching to the refernceSequence object e.g. to C3 but when we do the
            # blast they come up with C3 as their clostest and perfect match
            # To fix this we will run checkIfSeqInQHadRefSeqMatch

            if nodeFile[i][0] == '>':  # Then this is a def line
                sequenceInQ = nodeFile[i + 1]
                nodeNameInQ = nodeFile[i][1:].split('|')[0]
                # If True then node name will already have been associated to nodeToRefDict
                # and no need to do anything else
                found = checkIfSeqInQHadRefSeqMatch(seqInQ=sequenceInQ,
                                                    refSeqIdDict=reference_sequence_sequence_to_ID_dict,
                                                    nodeName=nodeNameInQ,
                                                    nodeToRefDict=nodeToRefDict,
                                                    refSeqIDNameDict=reference_sequence_ID_to_name_dict)

                if found == False:
                    # If there is no current match for the MED node in our current reference_sequences
                    # create a new reference_sequence object and add this to the refSeqDict
                    # Then assign the MED node to this new reference_sequence using the nodeToRefDict
                    newreferenceSequence = reference_sequence(clade=clade, sequence=sequenceInQ)
                    newreferenceSequence.save()
                    newreferenceSequence.name = str(newreferenceSequence.id)
                    newreferenceSequence.save()
                    listOfRefSeqs.append(newreferenceSequence)
                    reference_sequence_sequence_to_ID_dict[newreferenceSequence.sequence] = newreferenceSequence.id
                    nodeToRefDict[nodeNameInQ] = newreferenceSequence.id
                    reference_sequence_ID_to_name_dict[newreferenceSequence.id] = newreferenceSequence.name

                    sys.stdout.write('\rAssigning MED node {} to new reference sequence {}'.format(nodeFile[i][1:].split('|')[0],
                                                                                      newreferenceSequence.name))
        ########################################################################################

        # # Here we have a refSeq associated to each of the seqs found and we can now create dataSetSampleSequences that have associated referenceSequences
        # So at this point we have a reference_sequence associated with each of the nodes
        # Now it is time to define clade collections
        # Open the MED node count table as list of lists
        countArray = []
        nodes = []
        samples = []
        # this creates countArray which is a 2D list
        with open('MATRIX-COUNT.txt') as f:
            reader = csv.reader(f, delimiter='\t')
            countArray = list(reader)
        # get Nodes from first list
        nodes = countArray[0][1:]
        # del nodes
        del countArray[0]
        # get samples from first item of each list
        # del samples to leave raw numerical
        for i in range(len(countArray)):
            samples.append(countArray[i][0])
            del countArray[i][0]
        # convert to np array
        countArray = np.array(countArray)
        countArray = countArray.astype(np.int)
        # for each node in each sample create data_set_sample_sequence with foreign key to referenceSeq and data_set_sample
        # give it a foreign key to the reference Seq by looking up the seq in the dictionary made earlier and using the value to search for the referenceSeq



        for i in range(len(samples)):  # For each sample # There should only be one sample

            data_set_sample_object = data_set_sample.objects.get(dataSubmissionFrom=data_set.objects.get(id=ID),
                                                     name=samples[i])
            # Add the metadata to the data_set_sample
            data_set_sample_object.post_med_absolute += sum(countArray[i])
            data_set_sample_object.post_med_unique += len(countArray[i])
            data_set_sample_object.save()
            cladalSeqAbundanceCounter = [int(a) for a in json.loads(data_set_sample_object.cladalSeqTotals)]

            # This is where we need to tackle the issue of making sure we keep track of sequences in samples that
            # were not above the 200 threshold to be made into cladeCollections
            # We will simply add a list to the sampleObject that will be a sequence total for each of the clades
            # in order of cladeList

            # Here we modify the cladalSeqTotals string of the sample object to add the sequence totals
            # for the given clade
            cladeIndex = cladeList.index(clade)
            tempInt = cladalSeqAbundanceCounter[cladeIndex]
            tempInt += sum(countArray[i])
            cladalSeqAbundanceCounter[cladeIndex] = tempInt
            data_set_sample_object.cladalSeqTotals = json.dumps([str(a) for a in cladalSeqAbundanceCounter])
            data_set_sample_object.save()


            dssList = []

            if sum(countArray[i]) > 200:
                sys.stdout.write('\n{} clade {} sequences in {}. Creating clade_collection object\n'.format(sum(countArray[i]), sampleName, clade))
                newCC = clade_collection(clade=clade, dataSetSampleFrom=data_set_sample_object)
                newCC.save()
            else:
                sys.stdout.write(
                    '\n{} clade {} sequences in {}. Insufficient sequence to create a clade_collection object\n'.format(
                        sum(countArray[i]), clade, sampleName))

            # I want to address a problem we are having here. Now that we have thorough checks to
            # associate very similar sequences with indels by the primers to the same reference seq
            # it means that multiple sequences within the same sample can have the same referenceseqs
            # Due to the fact that we will in effect use the sequence of the reference seq rather
            # than the dsss seq, we should consolidate all dsss seqs with the same reference seq
            # so... we will create a counter that will keep track of the cumulative abundance associated with each reference_sequence
            # and then create a dsss for each refSeq from this.
            refSeqAbundanceCounter = defaultdict(int)
            for j in range(len(nodes)):
                abundance = countArray[i][j]
                if abundance > 0:
                    refSeqAbundanceCounter[reference_sequence.objects.get(id=nodeToRefDict[nodes[j]])] += abundance


            # > 200 associate a CC to the data_set_sample, else, don't
            # irrespective, associate a data_set_sample_sequences to the data_set_sample
            sys.stdout.write(
                '\nAssociating clade {} data_set_sample_sequences directly to data_set_sample {}\n'.format(clade, sampleName))
            if sum(countArray[i]) > 200:
                for refSeq in refSeqAbundanceCounter.keys():
                    dss = data_set_sample_sequence(referenceSequenceOf=refSeq,
                                                   cladeCollectionTwoFoundIn=newCC,
                                                   abundance=refSeqAbundanceCounter[refSeq],
                                                   data_set_sample_from=data_set_sample_object)
                    dssList.append(dss)
                # Save all of the newly created dss
                data_set_sample_sequence.objects.bulk_create(dssList)
                # Get the ids of each of the dss and add create a string of them and store it as cc.footPrint
                # This way we can quickly get the footprint of the CC.
                # Sadly we can't get eh IDs from the list so we will need to re-query
                # Instead we add the ID of each refseq in the refSeqAbundanceCounter.keys() list
                newCC.footPrint = ','.join([str(refSeq.id) for refSeq in refSeqAbundanceCounter.keys()])
                newCC.save()
            else:
                for refSeq in refSeqAbundanceCounter.keys():
                    dss = data_set_sample_sequence(referenceSequenceOf=refSeq,
                                                   abundance=refSeqAbundanceCounter[refSeq],
                                                   data_set_sample_from=data_set_sample_object)
                    dssList.append(dss)
                # Save all of the newly created dss
                data_set_sample_sequence.objects.bulk_create(dssList)

    return




def main(pathToInputFile, dSID, numProc, screen_sub_evalue=False,
         full_path_to_nt_database_directory='/home/humebc/phylogeneticSoftware/ncbi-blast-2.6.0+/ntdbdownload',
         data_sheet_path=None, noFig=False, noOrd=False):


    ############### UNZIP FILE, CREATE LIST OF SAMPLES AND WRITE stability.files FILE ##################

    dataSubmissionInQ = data_set.objects.get(id=dSID)
    cladeList = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I']
    if dataSubmissionInQ.initialDataProcessed == False:

        # Identify sample names and generate new stability file, generate data_set_sample objects in bulk
        wkd = generate_new_stability_file_and_data_set_sample_objects(cladeList, dSID, dataSubmissionInQ,
                                                                      data_sheet_path, pathToInputFile)

    ################### PERFORM pre-MED QC #################
        e_value_multiP_dict = preMED_QC(dSID, dataSubmissionInQ, numProc, wkd)

    # This function now performs the MEDs sample by sample, clade by clade.
    # The list of outputed paths lead to the MED directories where node info etc can be found
    sys.stdout.write('\n\nStarting MED analysis\n')
    MEDDirs = perform_MED(dataSubmissionInQ.workingDirectory, dataSubmissionInQ.id, numProc)

    create_data_set_sample_sequences_from_MED_nodes(dataSubmissionInQ.workingDirectory, dataSubmissionInQ.id, MEDDirs)

    # dataSubmissionInQ.dataProcessed = True
    dataSubmissionInQ.currentlyBeingProcessed = False
    dataSubmissionInQ.save()

    ### WRITE OUT REPORT OF HOW MANY SAMPLES WERE SUCCESSFULLY PROCESSED
    processed_samples_status(dataSubmissionInQ, pathToInputFile)

    # Here I also want to by default output a sequence drop that is a drop of the named sequences and their associated
    # sequences so that we mantain a link of the sequences to the names of the sequences
    perform_sequence_drop()


    ###### Assess and print out a fasta of the sequences that were found in multiple samples but were
    # below the e_value cutOff. Let's print these off in the directory that contains the data_set's
    # fastq.gz files. These will be screened later.
    fasta_out_with_clade = generate_and_write_below_evalue_fasta_for_screening(dSID, dataSubmissionInQ,
                                                                               e_value_multiP_dict, pathToInputFile,
                                                                               wkd)

    ##### CLEAN UP tempData FOLDER #####
    if os.path.exists(wkd):
        shutil.rmtree(wkd)

    ####### COUNT TABLE OUTPUT ########
    # We are going to make the sequence count table output as part of the dataSubmission
    outputDir = os.path.join(os.path.dirname(__file__), 'outputs/data_set_submissions/{}'.format(dSID))
    # the below method will create the tab delimited output table and print out the output file paths
    # it will also return these paths so that we can use them to grab the data for figure plotting
    output_path_list = div_output_pre_analysis_new_meta_and_new_dss_structure(datasubstooutput=str(dSID),
                                                           numProcessors=numProc,
                                                           output_dir=outputDir, call_type='submission')
    ###################################
    ####### Stacked bar output fig #####
    # here we will create a stacked bar
    # I think it is easiest if we directly pass in the path of the above count table output
    if not noFig:
        sys.stdout.write('\nGenerating sequence count table figures\n')
        for path in output_path_list:
            if 'relative' in path:
                path_to_rel_abund_data = path

        svg_path, png_path = generate_stacked_bar_data_submission(path_to_rel_abund_data, outputDir, dSID)
        sys.stdout.write('\nFigure generation complete')
        sys.stdout.write('\nFigures output to:')
        sys.stdout.write('\n{}'.format(svg_path))
        sys.stdout.write('\n{}'.format(png_path))



    ####### between sample distances ######
    if not noOrd:
        PCoA_paths_list = generate_within_clade_UniFrac_distances_samples(dataSubmission_str=dSID, num_processors=numProc,
                                                        method='mothur', call_type='submission', output_dir=outputDir)
        ####### distance plotting #############
        if not noFig:
            for pcoa_path in PCoA_paths_list:
                if 'PCoA_coords' in pcoa_path:
                    # then this is a full path to one of the .csv files that contains the coordinates that we can plot
                    # we will get the output directory from the passed in pcoa_path
                    sys.stdout.write('\n\nGenerating between sample distance plot clade {}\n'.format(os.path.dirname(pcoa_path).split('/')[-1]))
                    plot_between_sample_distance_scatter(pcoa_path)
        ####################################
    #######################################






    # write out whether there were below e value sequences outputted.
    if fasta_out_with_clade:
        print('\n\nWARNING: {} sub_e_value cut-off sequences were output'.format(int(len(fasta_out_with_clade)/2)))
    if screen_sub_evalue:
        if fasta_out_with_clade:
            print('These will now be automatically screened to see if they contain Symbiodinium sequences.')
            print('Screening sub e value sequences...')


        symportal_framework_object = symportal_framework.objects.get(id=1)
        preious_reference_fasta_name = symportal_framework_object.latest_reference_fasta
        required_sample_support = symportal_framework_object.required_sub_e_value_seq_support_samples
        required_symbiodinium_blast_matches = symportal_framework_object.required_sub_e_value_seq_support_blast_symbiodinium
        next_reference_fasta_iteration_id = symportal_framework_object.next_reference_fasta_iteration
        new_reference_fasta_name, num_additional_sequences, new_ref_fasta_location = screen_sub_e_value_sequences(dSID, pathToInputFile, iteration_id=next_reference_fasta_iteration_id, seq_sample_support_cut_off=required_sample_support, previous_reference_fasta_name=preious_reference_fasta_name, required_symbiodinium_matches=required_symbiodinium_blast_matches, full_path_to_nt_database_directory=full_path_to_nt_database_directory)

        print('Done\n')

        if new_reference_fasta_name:

            print('WARNING: {} Symbiodinium sequences were found in those discarded due to e value cutoffs.'.format(num_additional_sequences))
            print('A new reference fasta has been created that contains these new sequences as well as those that '
                  'were contained in the previous version of the reference fasta.')
            print('This new reference fasta is called: {}'.format(new_reference_fasta_name))
            print('It has been output to the following location: {}'.format(new_ref_fasta_location))
        else:
            print('None of the e value discarded sequences returned matches for Symbiodinium when run against '
                  'the nt database.\nHappy days!')
        print('data_set ID is: {}'.format(dataSubmissionInQ.id))
    else:
        if fasta_out_with_clade:
            print('A .fasta file containing the sub_e_values cut-off sequences was '
                  'output at {}'.format(pathToInputFile + '/below_e_cutoff_seqs_{}.fasta'.format(dSID)))
            print('These sequences were not submitted to your database as part of your data_set submission as SymPortal '
                  'could not be sure that they were truely Symbiodinium in origin')
            print('If you wish to include some of these sequences into your data_set submission please add them to '
                  'the ./symbiodiniumDB/symClade.fa fasta file and create a new BLAST datbase from this fasta with the '
                  'same name. Then re-run the submission')
            print('However, we strongly recommend that you verify these sequences to be of Symbiodinium origin before doing so.')

        print('\ndata_set ID is: {}'.format(dataSubmissionInQ.id))
    print('data_set submission complete')
    return dataSubmissionInQ.id


def generate_and_write_below_evalue_fasta_for_screening(dSID, dataSubmissionInQ, e_value_multiP_dict, pathToInputFile,
                                                        wkd):
    # make fasta from the dict
    fasta_out = make_evalue_screening_fasta_no_clade(dSID, e_value_multiP_dict, wkd)
    # we need to know what clade each of the sequences are
    # fastest way to do this is likely to run another blast on the symbiodinium clade reference dict
    fasta_out_with_clade = make_evalue_screening_fasta_with_clade(dataSubmissionInQ, fasta_out, wkd)
    # this will return a new fasta containing only the sequences that were 'Symbiodinium' matches
    # we can then output this dictionary
    writeListToDestination(pathToInputFile + '/below_e_cutoff_seqs_{}.fasta'.format(dSID), fasta_out_with_clade)
    return fasta_out_with_clade


def make_evalue_screening_fasta_with_clade(dataSubmissionInQ, fasta_out, wkd):
    ncbircFile = []
    db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'symbiodiniumDB'))
    ncbircFile.extend(["[BLAST]", "BLASTDB={}".format(db_path)])
    # write the .ncbirc file that gives the location of the db
    writeListToDestination("{0}/.ncbirc".format(wkd), ncbircFile)
    blastOutputPath = r'{}/blast.out'.format(wkd)
    outputFmt = "6 qseqid sseqid staxids evalue"
    inputPath = r'{}/blastInputFasta.fa'.format(wkd)
    os.chdir(wkd)
    # Run local blast
    # completedProcess = subprocess.run([blastnPath, '-out', blastOutputPath, '-outfmt', outputFmt, '-query', inputPath, '-db', 'symbiodinium.fa', '-max_target_seqs', '1', '-num_threads', '1'])
    completedProcess = subprocess.run(
        ['blastn', '-out', blastOutputPath, '-outfmt', outputFmt, '-query', inputPath, '-db',
         dataSubmissionInQ.reference_fasta_database_used,
         '-max_target_seqs', '1', '-num_threads', '1'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Read in blast output
    blast_output_file = readDefinedFileToList(r'{}/blast.out'.format(wkd))
    # now create a below_e_cutoff_seq to clade dictionary
    sub_e_seq_to_clade_dict = {a.split('\t')[0]: a.split('\t')[1][-1] for a in blast_output_file}
    # print out the fasta with the clades appended to the end
    fasta_out_with_clade = []
    for line in fasta_out:
        if line[0] == '>':
            fasta_out_with_clade.append(line + '_clade' + sub_e_seq_to_clade_dict[line[1:]])
        else:
            fasta_out_with_clade.append(line)
    return fasta_out_with_clade


def make_evalue_screening_fasta_no_clade(dSID, e_value_multiP_dict, wkd):
    below_e_cutoff_dict = dict(e_value_multiP_dict)
    temp_count = 0
    fasta_out = []
    for key, value in below_e_cutoff_dict.items():
        if value > 2:
            # then this is a sequences that was found in three or more samples
            fasta_out.extend(['>sub_e_seq_{}_{}_{}'.format(dSID, temp_count, value), key])
            temp_count += 1
    writeListToDestination(wkd + '/blastInputFasta.fa', fasta_out)
    return fasta_out


def perform_sequence_drop():
    sequence_drop_file = generate_sequence_drop_file()
    sequence_drop_path = os.path.dirname(__file__) + '/dbBackUp/seq_dumps/seq_dump_' + str(datetime.now())
    sys.stdout.write('\n\nBackup of named reference_sequences output to {}\n'.format(sequence_drop_path))
    writeListToDestination(sequence_drop_path, sequence_drop_file)


def processed_samples_status(dataSubmissionInQ, pathToInputFile):
    sampleList = data_set_sample.objects.filter(dataSubmissionFrom=dataSubmissionInQ)
    failedList = []
    for sample in sampleList:
        if sample.errorInProcessing:
            failedList.append(sample.name)
    readMeList = []
    sumMessage = '\n\n{0} out of {1} samples successfully passed QC.\n' \
                 '{2} samples produced erorrs\n'.format((len(sampleList) - len(failedList)), len(sampleList),
                                                        len(failedList))
    print(sumMessage)
    readMeList.append(sumMessage)
    for sample in sampleList:

        if sample.name not in failedList:
            print('Sample {} processed successfuly'.format(sample.name))
            readMeList.append('Sample {} processed successfuly'.format(sample.name))
        else:
            print('Sample {} : {}'.format(sample.name, sample.errorReason))
    for sampleName in failedList:
        readMeList.append('Sample {} : ERROR in sequencing reads. Unable to process'.format(sampleName))
    writeListToDestination(pathToInputFile + '/readMe.txt', readMeList)


def preMED_QC(dSID, dataSubmissionInQ, numProc, wkd):
    # check to see whether the reference_fasta_database_used has been created
    # we no longer by default have the blast binaries already made so that we don't have to have them up on
    # github. As such if this is the first time or if there has been an update of something
    # we should create the bast dictionary from the .fa
    validate_taxon_screening_ref_blastdb(dataSubmissionInQ)
    # this method will perform the bulk of the QC (everything before MED). The output e_value_mltiP_dict
    # will be used for screening the sequences that were found in multiple samples but were not close enough
    # to a sequence in the refreence database to be included outright in the analysis.
    e_value_multiP_dict = execute_preMED_worker(dSID, dataSubmissionInQ, numProc, wkd)
    ### We also need to set initialDataProcessed to True
    dataSubmissionInQ.initialDataProcessed = True
    dataSubmissionInQ.save()
    return e_value_multiP_dict


def execute_preMED_worker(dSID, dataSubmissionInQ, numProc, wkd):
    sampleFastQPairs = readDefinedFileToList(r'{0}/stability.files'.format(wkd))
    # Create the queues that will hold the sample information
    taskQueue = Queue()
    # Queue for output of successful and failed sequences
    outputQueue = Queue()
    # This will be a dictionary that we use to keep track of sequences that are found as matches when we do the
    # blast search against the symClade.fa database but that fall below the e value cutoff which is currently set
    # at e^-100. It will be a dictionary of sequence to number of samples in which the sequence was found in
    # the logic being that if we find sequences in multiple samples then they are probably genuine sequences
    # and they should therefore be checked against the full blast database to see if they match Symbiodinium
    # if they do then they should be put into the database.
    e_value_manager = Manager()
    e_value_multiP_dict = e_value_manager.dict()
    for contigPair in sampleFastQPairs:
        taskQueue.put(contigPair)
    for n in range(numProc):
        taskQueue.put('STOP')
    allProcesses = []
    # http://stackoverflow.com/questions/8242837/django-multiprocessing-and-database-connections
    db.connections.close_all()
    sys.stdout.write('\nPerforming QC\n')
    for n in range(numProc):
        p = Process(target=worker, args=(
        taskQueue, outputQueue, wkd, dSID, e_value_multiP_dict, dataSubmissionInQ.reference_fasta_database_used))
        allProcesses.append(p)
        p.start()
    for p in allProcesses:
        p.join()
    failedList = []
    outputQueue.put('STOP')
    for i in iter(outputQueue.get, 'STOP'):
        failedList.append(i)
    sys.stdout.write('\n\n{0} out of {1} samples successfully passed QC.\n'
                     '{2} samples produced erorrs\n'.format((len(sampleFastQPairs) - len(failedList)),
                                                            len(sampleFastQPairs), len(failedList)))
    for contigPair in sampleFastQPairs:
        sampleName = contigPair.split('\t')[0].replace('[dS]', '-')
        if sampleName not in failedList:
            print('Sample {} processed successfuly'.format(sampleName))
    for sampleName in failedList:
        print('Sample {} : ERROR in sequencing reads. Unable to process'.format(sampleName))
    return e_value_multiP_dict


def validate_taxon_screening_ref_blastdb(dataSubmissionInQ):
    list_of_binaries = [dataSubmissionInQ.reference_fasta_database_used + extension for extension in
                        ['.nhr', '.nin', '.nsq']]
    sym_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'symbiodiniumDB'))
    os.chdir(sym_dir)
    list_of_dir = os.listdir(sym_dir)
    binary_count = 0
    for item in list_of_dir:
        if item in list_of_binaries:
            binary_count += 1
    if binary_count != 3:
        # then some of the binaries are not present and we need to regenerate the blast dictionary
        # generate the blast dictionary again
        completed_process = subprocess.run(
            ['makeblastdb', '-in', dataSubmissionInQ.reference_fasta_database_used, '-dbtype', 'nucl', '-title',
             dataSubmissionInQ.reference_fasta_database_used.replace('.fa', '')], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)


def generate_new_stability_file_and_data_set_sample_objects(cladeList, dSID, dataSubmissionInQ, data_sheet_path,
                                                            pathToInputFile):
    # decompress (if necessary) and move the input files to the working directory
    wkd = copy_file_to_wkd(dSID, pathToInputFile)
    # Identify sample names and generate new stability file, generate data_set_sample objects in bulk
    if data_sheet_path:
        # if a data_sheet is provided ensure the samples names are derived from those in the data_sheet
        list_of_sample_objects = generate_stability_file_and_data_set_sample_objects_data_sheet(cladeList,
                                                                                                dataSubmissionInQ,
                                                                                                data_sheet_path, wkd)
    else:
        # if no data_sheet then infer the names of the samples from the .fastq.gz files
        list_of_sample_objects = generate_stability_file_and_data_set_sample_objects_inferred(cladeList,
                                                                                              dataSubmissionInQ,
                                                                                              wkd)
    # http://stackoverflow.com/questions/18383471/django-bulk-create-function-example
    smpls = data_set_sample.objects.bulk_create(list_of_sample_objects)
    return wkd


def generate_stability_file_and_data_set_sample_objects_inferred(cladeList, dataSubmissionInQ, wkd):
    # else, we have to infer what the samples names are
    # we do this by taking off the part of the fastq.gz name that samples have in common
    end_index, list_of_names = identify_sample_names_inferred(wkd)
    # Make a batch file for mothur, set input and output dir and create a .file file
    sampleFastQPairs = generate_mothur_dotfile_file(wkd)
    newstabilityFile = []
    # if we have a data_sheet_path then we will use the sample names that the user has associated to each
    # of the fastq pairs. We will use the fastq_file_to_sample_name_dict created above to do this
    # if we do not have a data_sheet path then we will get the sample name from the first
    # fastq using the end_index that we determined above
    generate_new_stability_file_inferred(end_index, newstabilityFile, sampleFastQPairs)
    # write out the new stability file
    writeListToDestination(r'{0}/stability.files'.format(wkd), newstabilityFile)
    sampleFastQPairs = newstabilityFile
    dataSubmissionInQ.workingDirectory = wkd
    dataSubmissionInQ.save()
    # Create data_set_sample instances
    list_of_sample_objects = []
    sys.stdout.write('\nCreating data_set_sample objects\n')
    for sampleName in list_of_names:
        print('\rCreating data_set_sample {}'.format(sampleName))
        # Create the data_set_sample objects in bulk.
        # The cladalSeqTotals property of the data_set_sample object keeps track of the seq totals for the
        # sample divided by clade. This is used in the output to keep track of sequences that are not
        # included in cladeCollections
        emptyCladalSeqTotals = json.dumps([0 for cl in cladeList])

        dss = data_set_sample(name=sampleName, dataSubmissionFrom=dataSubmissionInQ,
                              cladalSeqTotals=emptyCladalSeqTotals)
        list_of_sample_objects.append(dss)
    return list_of_sample_objects


def generate_stability_file_and_data_set_sample_objects_data_sheet(cladeList, dataSubmissionInQ, data_sheet_path, wkd):
    # Create a pandas df from the data_sheet if it was provided
    sample_meta_df = pd.read_excel(io=data_sheet_path, header=0, index_col=0, usecols='A:N', skiprows=[0])
    # if we are given a data_sheet then use these sample names given as the data_set_sample object names
    fastq_file_to_sample_name_dict, list_of_names = identify_sample_names_data_sheet(sample_meta_df, wkd)
    # Make a batch file for mothur, set input and output dir and create a .file file
    sampleFastQPairs = generate_mothur_dotfile_file(wkd)
    newstabilityFile = []
    # if we have a data_sheet_path then we will use the sample names that the user has associated to each
    # of the fastq pairs. We will use the fastq_file_to_sample_name_dict created above to do this
    # if we do not have a data_sheet path then we will get the sample name from the first
    # fastq using the end_index that we determined above
    generate_new_stability_file_data_sheet(fastq_file_to_sample_name_dict, newstabilityFile, sampleFastQPairs)
    # write out the new stability file
    writeListToDestination(r'{0}/stability.files'.format(wkd), newstabilityFile)
    sampleFastQPairs = newstabilityFile
    dataSubmissionInQ.workingDirectory = wkd
    dataSubmissionInQ.save()
    # Create data_set_sample instances
    list_of_sample_objects = []
    sys.stdout.write('\nCreating data_set_sample objects\n')
    for sampleName in list_of_names:
        print('\rCreating data_set_sample {}'.format(sampleName))
        # Create the data_set_sample objects in bulk.
        # The cladalSeqTotals property of the data_set_sample object keeps track of the seq totals for the
        # sample divided by clade. This is used in the output to keep track of sequences that are not
        # included in cladeCollections
        emptyCladalSeqTotals = json.dumps([0 for cl in cladeList])

        dss = data_set_sample(name=sampleName, dataSubmissionFrom=dataSubmissionInQ,
                              cladalSeqTotals=emptyCladalSeqTotals,
                              sample_type=sample_meta_df.loc[sampleName, 'sample_type'],
                              host_phylum=sample_meta_df.loc[sampleName, 'host_phylum'],
                              host_class=sample_meta_df.loc[sampleName, 'host_class'],
                              host_order=sample_meta_df.loc[sampleName, 'host_order'],
                              host_family=sample_meta_df.loc[sampleName, 'host_family'],
                              host_genus=sample_meta_df.loc[sampleName, 'host_genus'],
                              host_species=sample_meta_df.loc[sampleName, 'host_species'],
                              collection_latitude=sample_meta_df.loc[sampleName, 'collection_latitude'],
                              collection_longitude=sample_meta_df.loc[sampleName, 'collection_longitude'],
                              collection_date=sample_meta_df.loc[sampleName, 'collection_date'],
                              collection_depth=sample_meta_df.loc[sampleName, 'collection_depth']
                              )
        list_of_sample_objects.append(dss)
    return list_of_sample_objects


def generate_new_stability_file_inferred(end_index, newstabilityFile, sampleFastQPairs):
    for stability_file_line in sampleFastQPairs:
        pairComponenets = stability_file_line.split('\t')
        # I am going to use '[dS]' as a place holder for a dash in the sample names
        # Each line of the stability file is a three column format with the first
        # column being the sample name. The second and third are the full paths of the .fastq.gz files
        # the sample name at the moment is garbage, we will extract the sample name from the
        # first fastq path using the end_index that we determined above

        newstabilityFile.append(
            '{}\t{}\t{}'.format(
                pairComponenets[1].split('/')[-1][:-end_index].replace('-', '[dS]'),
                pairComponenets[1],
                pairComponenets[2]))


def generate_new_stability_file_data_sheet(fastq_file_to_sample_name_dict, newstabilityFile, sampleFastQPairs):
    for stability_file_line in sampleFastQPairs:
        pairComponenets = stability_file_line.split('\t')
        # I am going to use '[dS]' as a place holder for a dash in the sample names
        # Each line of the stability file is a three column format with the first
        # column being the sample name. The second and third are the full paths of the .fastq.gz files
        # the sample name at the moment is garbage, we will identify the sample name from the
        # first fastq path using the fastq_file_to_sample_name_dict

        newstabilityFile.append(
            '{}\t{}\t{}'.format(
                fastq_file_to_sample_name_dict[pairComponenets[1].split('/')[-1]].replace('-', '[dS]'),
                pairComponenets[1],
                pairComponenets[2]))


def generate_mothur_dotfile_file(wkd):
    mBatchFile = [
        r'set.dir(input={0})'.format(wkd),
        r'set.dir(output={0})'.format(wkd),
        r'make.file(inputdir={0}, type=gz, numcols=3)'.format(wkd)
    ]
    writeListToDestination(r'{0}/mBatchFile_makeFile'.format(wkd), mBatchFile)
    completedProcess = subprocess.run(['mothur', r'{0}/mBatchFile_makeFile'.format(wkd)], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE)
    # Convert the group names in the stability.files so that the dashes are converted to '[ds]',
    # So for the mothur we have '[ds]'s. But for all else we convert these '[ds]'s to dashes
    sampleFastQPairs = readDefinedFileToList(r'{0}/stability.files'.format(wkd))
    return sampleFastQPairs


def identify_sample_names_inferred(wkd):
    list_of_gz_files_in_wkd = [a for a in os.listdir(wkd) if '.gz' in a]
    # I think the simplest way to get sample names is to find what parts are common between all samples
    # well actually 50% of the samples so that we also remove the R1 and R2 parts.
    i = 1
    while 1:
        list_of_endings = []
        for file in list_of_gz_files_in_wkd:
            list_of_endings.append(file[-i:])
        if len(set(list_of_endings)) > 2:
            break
        else:
            i += 1
            # then this is one i too many and our magic i was i-1
    end_index = i - 1
    list_of_names_non_unique = []
    for file in list_of_gz_files_in_wkd:
        list_of_names_non_unique.append(file[:-end_index])
    list_of_names = list(set(list_of_names_non_unique))
    if len(list_of_names) != len(list_of_gz_files_in_wkd) / 2:
        sys.exit('Error in sample name extraction')
    return end_index, list_of_names


def identify_sample_names_data_sheet(sample_meta_df, wkd):
    # get the list of names from the index of the sample_meta_df
    list_of_names = sample_meta_df.index.values.tolist()
    # we will also need to know how to relate the samples to the fastq files
    # for this we will make a dict of fastq file name to sample
    # but before we do this we should verify that all of the fastq files listed in the sample_meta_df
    # are indeed found in the directory that we've been given
    list_of_gz_files_in_wkd = [a for a in os.listdir(wkd) if '.gz' in a]
    list_of_meta_gz_files = []
    list_of_meta_gz_files.extend(sample_meta_df['fastq_fwd_file_name'].values.tolist())
    list_of_meta_gz_files.extend(sample_meta_df['fastq_rev_file_name'].values.tolist())
    for fastq in list_of_meta_gz_files:
        if fastq not in list_of_gz_files_in_wkd:
            sys.exit('{} listed in data_sheet not found'.format(fastq, wkd))
    # now make the dictionary
    fastq_file_to_sample_name_dict = {}
    for sample_index in sample_meta_df.index.values.tolist():
        fastq_file_to_sample_name_dict[sample_meta_df.loc[sample_index, 'fastq_fwd_file_name']] = sample_index
        fastq_file_to_sample_name_dict[sample_meta_df.loc[sample_index, 'fastq_rev_file_name']] = sample_index
    return fastq_file_to_sample_name_dict, list_of_names


def copy_file_to_wkd(dSID, pathToInputFile):
    # working directory will be housed in a temp folder within the directory in which the sequencing data
    # is currently housed
    if '.' in pathToInputFile.split('/')[-1]:
        # then this path points to a file rather than a directory and we should pass through the path only
        wkd = os.path.abspath('{}/tempData/{}'.format(os.path.dirname(pathToInputFile), dSID))
    else:
        # then we assume that we are pointing to a directory and we can directly use that to make the wkd
        wkd = os.path.abspath('{}/tempData/{}'.format(pathToInputFile, dSID))
    # if the directory already exists remove it and start from scratch
    if os.path.exists(wkd):
        shutil.rmtree(wkd)
    os.makedirs(wkd)
    # Check to see if the files are already decompressed
    # If so then simply copy the files over to the destination folder
    # we do this copying so that we don't corrupt the original files
    # we will delte these duplicate files after processing
    compressed = True
    for file in os.listdir(pathToInputFile):
        if 'fastq.gz' in file or 'fq.gz' in file:
            # Then there is a fastq.gz already uncompressed in this folder
            # In this case we will assume that the seq data is not compressed into a master .zip or .gz
            # Copy to the wkd
            compressed = False
            os.chdir('{}'.format(pathToInputFile))

            # * asterix are only expanded in the shell and so don't work through subprocess
            # need to use the glob library instead
            # https://stackoverflow.com/questions/13875978/python-subprocess-popen-why-does-ls-txt-not-work

            if 'fastq.gz' in file:
                completedProcess = subprocess.run(['cp'] + glob.glob('*.fastq.gz') + [wkd], stdout=subprocess.PIPE,
                                                  stderr=subprocess.PIPE)
            elif 'fq.gz' in file:
                completedProcess = subprocess.run(['cp'] + glob.glob('*.fq.gz') + [wkd], stdout=subprocess.PIPE,
                                                  stderr=subprocess.PIPE)
            break
    # if compressed then we are dealing with a single compressed file that should contain the fastq.gz pairs
    # Decompress the file to destination
    if compressed:
        extComponents = pathToInputFile.split('.')
        if extComponents[-1] == 'zip':  # .zip
            completedProcess = subprocess.run(["unzip", pathToInputFile, '-d', wkd], stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE)
        elif extComponents[-2] == 'tar' and extComponents[-1] == 'gz':  # .tar.gz
            completedProcess = subprocess.run(["tar", "-xf", pathToInputFile, "-C", wkd], stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE)
        elif extComponents[-1] == 'gz':  # .gz
            completedProcess = subprocess.run(["gunzip", "-c", pathToInputFile, ">", wkd], stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE)
    return wkd


def screen_sub_e_value_sequences(ds_id, data_sub_data_dir, iteration_id, seq_sample_support_cut_off, previous_reference_fasta_name, required_symbiodinium_matches, full_path_to_nt_database_directory):
    # we need to make sure that we are looking at matches that cover > 95%
    # this is probably the most important point. We can then decide what percentage coverage we want
    # perhaps something like 60%.
    # we then need to see if there is a 'Symbiodinium' sequence that matches the query and all of these
    # requirements. If so then we consider the sequence to be Symbiodinium
    # TODO make sure that we have metrics that show how many sequences were kicked out for each iterarion that we
    # do the database update.
    # We should write out the new database with an iteration indicator so that we can keep track of the progress of the
    # database creations. We can then run the database submissions using specific iterations of the symclade dataase
    # we can name the data_set that we do so that they can link in with which database iteration they are using

    # we can work with only seuqences that are found above a certain level of support. We can use the
    # seq_sample_support_cut_off for this.


    # Write out the hidden file that points to the ncbi database directory.
    ncbircFile = []
    # db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'symbiodiniumDB'))

    db_path = full_path_to_nt_database_directory
    ncbircFile.extend(["[BLAST]", "BLASTDB={}".format(db_path)])
    writeListToDestination("{}.ncbirc".format(data_sub_data_dir + '/'), ncbircFile)


    # Read in the fasta files of below e values that were kicked out.
    fasta_file = readDefinedFileToList('{}/below_e_cutoff_seqs_{}.fasta'.format(data_sub_data_dir, ds_id))
    fasta_file_dict = createDictFromFasta(fasta_file)

    # screen the input fasta for sample support according to seq_sample_support_cut_off
    screened_fasta = []
    for i in range(len(fasta_file)):
        if fasta_file[i][0] == '>':
            if int(fasta_file[i].split('_')[5]) >= seq_sample_support_cut_off:
                screened_fasta.extend([fasta_file[i], fasta_file[i + 1]])

    # write out the screened fasta so that it can be read in to the blast
    # make sure to reference the sequence support and the iteration
    path_to_screened_fasta = '{}/{}_{}_{}.fasta'.format(data_sub_data_dir,'below_e_cutoff_seqs_{}.screened'.format(ds_id), iteration_id, seq_sample_support_cut_off)
    screened_fasta_dict = createDictFromFasta(screened_fasta)
    writeListToDestination(path_to_screened_fasta, screened_fasta)

    # Set up environment for running local blast
    blastOutputPath = r'{}/blast_{}_{}.out'.format(data_sub_data_dir, iteration_id, seq_sample_support_cut_off)
    outputFmt = "6 qseqid sseqid staxids evalue pident qcovs staxid stitle ssciname"
    # inputPath = r'{}/below_e_cutoff_seqs.fasta'.format(data_sub_data_dir)
    os.chdir(data_sub_data_dir)

    # Run local blast
    # completedProcess = subprocess.run([blastnPath, '-out', blastOutputPath, '-outfmt', outputFmt, '-query', inputPath, '-db', 'symbiodinium.fa', '-max_target_seqs', '1', '-num_threads', '1'])
    completedProcess = subprocess.run(
        ['blastn', '-out', blastOutputPath, '-outfmt', outputFmt, '-query', path_to_screened_fasta, '-db', 'nt',
         '-max_target_seqs', '10', '-num_threads', '20'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # Read in blast output
    blast_output_file = readDefinedFileToList(r'{}/blast_{}_{}.out'.format(data_sub_data_dir, iteration_id, seq_sample_support_cut_off))

    # blastDict = {a.split('\t')[0]: a.split('\t')[1][-1] for a in blastOutputFile}
    apples = 'asdf'
    # Now go through each of the results and look to see if there is a result that has > 95% coverage and has >60%
    # match and has symbiodinium in the name.
    # if you find one then add the name of this seq to the reference db

    # create a dict that is the query name key and a list of subject return value
    blast_output_dict = defaultdict(list)
    for line in blast_output_file:
        blast_output_dict[line.split('\t')[0]].append('\t'.join(line.split('\t')[1:]))

    verified_sequence_list = []
    for k, v in blast_output_dict.items():
        sym_count = 0
        for result_str in v:
            if 'Symbiodinium' in result_str:
                percentage_coverage = float(result_str.split('\t')[4])
                percentage_identity_match = float(result_str.split('\t')[3])
                if percentage_coverage > 95 and percentage_identity_match > 60:
                    sym_count += 1
                    if sym_count == required_symbiodinium_matches:
                        verified_sequence_list.append(k)
                        break

    # We only need to proceed from here to make a new database if we have sequences that ahve been verified as
    # Symbiodinium
    if verified_sequence_list:
        # here we have a list of the Symbiodinium sequences that we can add to the reference db fasta
        new_fasta = []
        for seq_to_add in verified_sequence_list:
            new_fasta.extend(['>{}'.format(seq_to_add), '{}'.format(screened_fasta_dict[seq_to_add])])

        # now add the current sequences
        previous_reference_fasta = readDefinedFileToList('{}/{}'.format(os.path.abspath(os.path.join(os.path.dirname(__file__), 'symbiodiniumDB')), previous_reference_fasta_name))
        # we need to check that none of the new sequence names are found in
        new_fasta += previous_reference_fasta

        # now that the reference db fasta has had the new sequences added to it.
        # write out to the db to the database directory of SymPortal
        full_path_to_new_ref_fasta_iteration = '{}/symClade_{}_{}.fa'.format(os.path.abspath(os.path.join(os.path.dirname(__file__), 'symbiodiniumDB')), iteration_id, seq_sample_support_cut_off)
        writeListToDestination(full_path_to_new_ref_fasta_iteration, new_fasta)

        # now update the SymPortal framework object
        symportal_framework_object = symportal_framework.objects.get(id=1)
        symportal_framework_object.latest_reference_fasta = 'symClade_{}_{}.fa'.format(iteration_id, seq_sample_support_cut_off)
        symportal_framework_object.next_reference_fasta_iteration += 1
        symportal_framework_object.save()

        # run makeblastdb
        completed_process = subprocess.run(['makeblastdb','-in', full_path_to_new_ref_fasta_iteration,'-dbtype' ,'nucl', '-title', 'symClade_{}_{}'.format(iteration_id, seq_sample_support_cut_off)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        return 'symClade_{}_{}.fa'.format(iteration_id, seq_sample_support_cut_off), len(verified_sequence_list), full_path_to_new_ref_fasta_iteration
    else:
        return False, 0, False

def generate_sequence_drop_file():
    # this will simply produce a list
    # in the list each item will be a line of text that will be a refseq name, clade and sequence
    output_list = []
    for ref_seq in reference_sequence.objects.filter(hasName=True):
        output_list.append('{}\t{}\t{}'.format(ref_seq.name, ref_seq.clade, ref_seq.sequence))
    return output_list




