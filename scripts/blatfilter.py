#!/usr/bin/env python

''' QC script using BLAT w/ consensus breakpoints '''

import sys
import os
import traceback
import argparse
import subprocess
import datetime

from collections import OrderedDict as od
from uuid import uuid4
from time import sleep
from math import log


class PSL:
    def __init__(self, rec):
        # psl spec: http://www.ensembl.org/info/website/upload/psl.html
        (self.matches, self.misMatches, self.repMatches, self.nCount, self.qNumInsert, self.qBaseInsert, 
         self.tNumInsert, self.tBaseInsert, self.strand, self.qName, self.qSize, self.qStart, self.qEnd,
         self.tName, self.tSize, self.tStart, self.tEnd, self.blockCount, self.blockSizes, self.qStarts,
         self.tStarts) = rec.strip().split()

        self.tName = self.tName.replace('chr', '')

        self.tStart, self.tEnd, self.qStart, self.qEnd = map(int, (self.tStart, self.tEnd, self.qStart, self.qEnd))
        
        if self.qStart > self.qEnd:
            self.qStart, self.qEnd = self.qEnd, self.qStart

        if self.tStart > self.tEnd:
            self.tStart, self.tEnd = self.tEnd, self.tStart

    def match(self, chrom, pos, window=0):
        ''' return True if chrom:pos intersects BLAT hit +/- window '''
        chrom = chrom.replace('chr', '')
        if chrom != self.tName:
            return False

        if int(pos) >= int(self.tStart)-window and int(pos) <= int(self.tEnd)+window:
            return True

        return False

    def refspan(self):
        ''' return footprint of match relative to referece genome '''
        return self.tEnd - self.tStart

    def score(self):
        ''' adapted from https://genome.ucsc.edu/FAQ/FAQblat.html#blat4 '''
        return (int(self.matches) + (int(self.repMatches)>>1)) - int(self.misMatches) - int(self.qNumInsert) - int(self.tNumInsert)

    def pctmatch(self):
        ''' adapted from https://genome.ucsc.edu/FAQ/FAQblat.html#blat4 '''
        qAliSize = int(self.qEnd) - int(self.qStart)
        tAliSize = int(self.tEnd) - int(self.tStart)
        if min(qAliSize, tAliSize) <= 0:
            return 0.0

        sizeDif = abs(qAliSize - tAliSize)
        total = int(self.matches) + int(self.repMatches) + int(self.misMatches)

        if total > 0:
            return 1.0-float((int(self.misMatches) + int(self.qNumInsert) + int(self.tNumInsert) + round(3*log(1+sizeDif)))) / float(total)

        return 0.0

    def __lt__(self, other):
        ''' used for ranking BLAT hits '''
        return self.score() < other.score()


def now():
    return str(datetime.datetime.now())


def start_blat_server(blatref, port=9999):
    # parameters from https://genome.ucsc.edu/FAQ/FAQblat.html#blat5

    cmd = ['gfServer', 'start', 'localhost', str(port), '-stepSize=5', blatref]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    dummy_fa  = '/tmp/' + str(uuid4()) + '.fa'
    dummy_psl = dummy_fa.replace('.fa', '.psl')

    with open(dummy_fa, 'w') as dout:
        dout.write('>\n' + 'A'*100)

    server_up = False

    poll_cmd = ['gfClient', 'localhost', str(port), blatref, dummy_fa, dummy_psl]
    poll_time = 10

    while not server_up:
        started = True
        t = subprocess.Popen(poll_cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE)

        for line in t.stderr:
            if line.startswith('Sorry'):
                started = False

        if not started:
            print "waiting for BLAT server to start for " + blatref + " ... ", now()
            sleep(poll_time)
        else:
            server_up=True
            print "BLAT for " + blatref + " server up with PID:", p.pid

    return p


def blat(fasta, blatref, outpsl, port=9999, minScore=0, maxIntron=None):
    ''' BLAT using gfClient utility '''
    cmd  = ['gfClient', 'localhost', str(port), '-nohead']

    if maxIntron is not None:
        cmd.append('-maxIntron=' + str(maxIntron))

    if minScore is not None:
        cmd.append('-minScore=' + str(minScore))

    cmd += ['/', fasta, outpsl]
    p = subprocess.call(cmd)


def ref_parsepsl(psl, chrom, pos):
    recs = []
    with open(psl, 'r') as inpsl:
        for line in inpsl:
            rec = PSL(line)
            if rec.match(chrom, pos):
                recs.append(rec)
    return sorted(recs)


def te_parsepsl(psl):
    recs = []
    with open(psl, 'r') as inpsl:
        recs = sorted([PSL(line) for line in inpsl])
    return recs


def checkmap(maptabix, chrom, start, end):
    ''' return average mappability across chrom:start-end region '''
    scores = []

    if chrom in maptabix.contigs:
        for rec in maptabix.fetch(chrom, int(start), int(end)):
            mchrom, mstart, mend, mscore = rec.strip().split()
            mstart, mend = int(mstart), int(mend)
            mscore = float(mscore)

            while mstart < mend and mstart:
                mstart += 1
                if mstart >= int(start) and mstart <= int(end):
                    scores.append(mscore)

        if len(scores) > 0:
            return sum(scores) / float(len(scores))
        else:
            return 0.0
    else:
        return 0.0


def checkseq(cons, chrom, pos, genomeref, teref, refport, teport, maptabix=None):
    ''' find breakpoint chrom:pos in BLAT output '''
    pos = int(pos)
    chrom = chrom.replace('chr', '')

    fa = '/tmp/' + str(uuid4()) + '.fa'
    ref_psl = fa.replace('.fa', '.ref.psl')
    te_psl = fa.replace('.fa', '.te.psl')

    with open(fa, 'w') as tmpfa:
        tmpfa.write('>' + chrom + ':' + str(pos) + '\n' + cons + '\n')

    blat(fa, genomeref, ref_psl, port=refport, minScore=0)
    blat(fa, teref, te_psl, port=teport, maxIntron=2)
    ref_recs = ref_parsepsl(ref_psl, chrom, pos)
    te_recs  = te_parsepsl(te_psl)

    data = od()
    data['pass'] = True # default

    # Filters
    if len(ref_recs) == 0 or len(te_recs) == 0:
        data['pass'] = False
        return data

    else:
        data['tematch']     = te_recs[0].pctmatch()
        data['refmatch']    = ref_recs[0].pctmatch()
        data['refmatchlen'] = int(ref_recs[0].tEnd) - int(ref_recs[0].tStart)
        data['refquerylen'] = int(ref_recs[0].qEnd) - int(ref_recs[0].qStart)
        data['tematchlen']  = int(te_recs[0].tEnd) - int(te_recs[0].tStart)
        data['tequerylen']  = int(te_recs[0].qEnd) - int(te_recs[0].qStart)
        data['overlap']     = min(te_recs[0].qEnd, ref_recs[0].qEnd) - max(te_recs[0].qStart, ref_recs[0].qStart)
        data['refqstart']   = ref_recs[0].qStart
        data['refqend']     = ref_recs[0].qEnd
        data['teqstart']    = te_recs[0].qStart
        data['teqend']      = te_recs[0].qEnd
        data['teclass']     = te_recs[0].tName.split(':')[0]
        data['tefamily']    = te_recs[0].tName.split(':')[-1]

        if maptabix is not None:
            if chrom in maptabix.contigs:
                data['avgmap'] = checkmap(maptabix, chrom, int(ref_recs[0].tStart), int(ref_recs[0].tEnd))

    if data['tematch'] < 0.95:
        data['pass'] = False

    if data['refmatch'] < 0.95:
        data['pass'] = False

    if data['refmatchlen'] > len(cons):
        data['pass'] = False

    if maptabix is not None:
        if data['avgmap'] < 0.85:
            data['pass'] = False

    if data['tematchlen'] < 30:
        data['pass'] = False

    os.remove(fa)
    #os.remove(ref_psl)
    os.remove(te_psl)

    return data


def main(args):
    chrom1, pos1 = args.pos1.split(':')

    p = start_blat_server(args.genomeref, port=args.refport)
    t = start_blat_server(args.teref, port=args.teport)

    try:
        data = checkseq(args.seq1, chrom1, pos1, args.genomeref, args.teref, args.refport, args.teport)
        print data
    except Exception, e:
        sys.stderr.write("*"*60 + "\nerror in blat filter:\n")
        traceback.print_exc(file=sys.stderr)
        sys.stderr.write("*"*60 + "\n")

    print "killing BLAT server(s) ..."
    p.kill()
    t.kill()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='BLAT filter')
    parser.add_argument('--seq1', required=True, help='consensus sequence')
    parser.add_argument('--seq2', default=None, help='consensus sequence')
    parser.add_argument('--pos1', required=True, help='postion formatted as chrom:pos')
    parser.add_argument('--pos2', default=None, help='postion formatted as chrom:pos')
    parser.add_argument('--genomeref', required=True, help='BLAT reference')
    parser.add_argument('--teref', required=True, help='TE reference')
    parser.add_argument('--refport', default=9999)
    parser.add_argument('--teport', default=9998)
    args = parser.parse_args()
    main(args)
