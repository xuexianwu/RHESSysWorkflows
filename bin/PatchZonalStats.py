#!/usr/bin/env python
"""@package PatchZonalStats

@brief Tool for calculating zonal statistics for patch-scale RHESSys output variables.

This software is provided free of charge under the New BSD License. Please see
the following license information:

Copyright (c) 2014, University of North Carolina at Chapel Hill
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
    * Redistributions of source code must retain the above copyright
      notice, this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright
      notice, this list of conditions and the following disclaimer in the
      documentation and/or other materials provided with the distribution.
    * Neither the name of the University of North Carolina at Chapel Hill nor the
      names of its contributors may be used to endorse or promote products
      derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE UNIVERSITY OF NORTH CAROLINA AT CHAPEL HILL
BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR 
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE
GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT 
LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT
OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


@author Brian Miles <brian_miles@unc.edu>


Pre conditions
--------------
1. Configuration file must define the following sections and values:
   'GRASS', 'GISBASE'

2. The following metadata entry(ies) must be present in the RHESSys section of the metadata associated with the project directory:
   grass_dbase
   grass_location
   grass_mapset


Post conditions
---------------
None

"""
import os, sys, shutil, tempfile, re
import subprocess, shlex
import time, random
import argparse
import operator

import numpy as np
import statsmodels.api as sm
import matplotlib.pyplot as plt

from ecohydrolib.context import Context
from rhessysworkflows.metadata import RHESSysMetadata
from ecohydrolib.grasslib import *

from rhessysworkflows.rhessys import RHESSysOutput

def simple_axis(ax):
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.get_xaxis().tick_bottom()
    ax.get_yaxis().tick_left()

def plot_cdf(ax, data, numbins=1000, xlabel=None, ylabel=None, title=None):    
    (n, bins, patches) = \
        ax.hist(data, numbins, normed=True, cumulative=True, stacked=True,
                histtype='step')
    # Remove last point in graph to that the end of the graph doesn't
    # go to y=0
    patches[0].set_xy(patches[0].get_xy()[:-1])
    simple_axis(ax)
    
    if xlabel:
        ax.set_xlabel(xlabel, fontsize=8)
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=8)
    if title:
        ax.set_title(title)
    
    ax.set_xlim(right=bins[-2])
    ax.set_ylim(0, 1)
    plt.setp(ax.get_xticklabels(), fontsize=6)
    plt.setp(ax.get_yticklabels(), fontsize=6)

INT_RESCALE = 10 * 1000
VARIABLE_EXPR_RE = re.compile(r'\b([a-zA-z][a-zA-Z0-9_\.]+)\b') # variable names can have "." in them
RANDOM = random.randint(100000, 999999)
RECLASS_MAP_TMP = "patchzonalstats_cover_{0}".format(RANDOM)
STATS_MAP_TMP = "patchzonalstats_output_{0}".format(RANDOM)

methods = ['average', 'mode', 'median', 'avedev', 'stddev', 'variance',
           'skewness', 'kurtosis', 'min', 'max', 'sum']

# Handle command line options
parser = argparse.ArgumentParser(description='Generate cummulative map of patch-scale RHESSys output variables')
parser.add_argument('-i', '--configfile', dest='configfile', required=False,
                    help='The configuration file.')
parser.add_argument('-p', '--projectDir', dest='projectDir', required=True,
                    help='The directory to which metadata, intermediate, and final files should be saved')
parser.add_argument('-d', '--rhessysOutFile', required=True, nargs='+',
                    help='Directory containing RHESSys patch output, ' +\
                         'specificically patch yearly output in a file ending in "_patch.yearly".')
parser.add_argument('--mask', required=False, default=None,
                    help='Name of raster to use as a mask')
parser.add_argument('-z', '--zones', required=True,
                    help='Name of raster to use as zones in which statistic is to be calculated')
parser.add_argument('-o', '--outputDir', required=True,
                    help='Directory to which map should be output')
parser.add_argument('-f', '--outputFile', required=True, nargs='+',
                    help='Name(s) of file(s) to store plot(s) in; ".pdf" will be appended. If file exists it will be overwritten.')
parser.add_argument('--patchMap', required=False, default='patch',
                    help='Name of patch map')
parser.add_argument('-y', '--year', required=False, type=int,
                    help='Year for which statistics should be generated')
parser.add_argument('-v', '--outputVariable', required=True,
                    help='Name of RHESSys variable to be mapped.  Can be an expression such as "trans_sat + trans_unsat"')
parser.add_argument('-n' ,'--variableName', required=False,
                    help='Name to use for variable.  If not supplied, outputVariable will be used')
parser.add_argument('-s', '--statistic', required=True,
                    choices=methods,
                    help='Statistic to calculate')
parser.add_argument('--keepmap', required=False, action='store_true', default=True,
                    help='Whether to keep resulting spatial statistics GRASS map')
args = parser.parse_args()

configFile = None
if args.configfile:
    configFile = args.configfile

context = Context(args.projectDir, configFile)

if len(args.rhessysOutFile) != len(args.outputFile):
    sys.exit("Number of data files %d does not match number of output filenames %d" % \
             (len(args.rhessysOutFile), len(args.outputFile)) )

# Check for necessary information in metadata
metadata = RHESSysMetadata.readRHESSysEntries(context)
if not 'grass_dbase' in metadata:
    sys.exit("Metadata in project directory %s does not contain a GRASS Dbase" % (context.projectDir,)) 
if not 'grass_location' in metadata:
    sys.exit("Metadata in project directory %s does not contain a GRASS location" % (context.projectDir,)) 
if not 'grass_mapset' in metadata:
    sys.exit("Metadata in project directory %s does not contain a GRASS mapset" % (context.projectDir,))

patchFilepaths = []
for outfile in args.rhessysOutFile:
    if not os.path.isfile(outfile) or not os.access(outfile, os.R_OK):
        sys.exit("Unable to read RHESSys output file %s" % (outfile,))
    patchFilepaths.append( os.path.abspath(outfile) ) 

outputFilePaths = []
for outfile in args.outputFile:
    if not os.path.isdir(args.outputDir) or not os.access(args.outputDir, os.W_OK):
        sys.exit("Unable to write to output directory %s" % (args.outputDir,) )
    outputDir = os.path.abspath(args.outputDir)
    outputFile = "%s.pdf" % (outfile,)
    outputFilePaths.append( os.path.join(outputDir, outputFile) )

variableLabel = args.outputVariable
if args.variableName:
    variableLabel = args.variableName

# Determine output variables
variables = ['patchID']
m = VARIABLE_EXPR_RE.findall(args.outputVariable)
if m:
    variables += m
else:
    sys.exit("No output variables specified")

# 1. Get tmp folder for temprarily storing rules
tmpDir = tempfile.mkdtemp()
print("Temp dir: %s" % (tmpDir,) )
reclassRule = os.path.join(tmpDir, 'reclass.rule')

# 2. Initialize GRASS
grassDbase = os.path.join(context.projectDir, metadata['grass_dbase'])
grassConfig = GRASSConfig(context, grassDbase, metadata['grass_location'], metadata['grass_mapset'])
grassLib = GRASSLib(grassConfig=grassConfig)

# Set mask (if present)
if args.mask:
    result = grassLib.script.run_command('r.mask',
                                         input=args.mask,
                                         maskcats="1",
                                         flags="o")
    if result != 0:
        sys.exit("Failed to set mask using layer %s" % (args.mask,) )
    # Zoom to map extent
    result = grassLib.script.run_command('g.region',
                                         rast='MASK',
                                         zoom='MASK')
    if result != 0:
        sys.exit("Failed to set region to layer %s" % \
                 (args.mask,) )

# 3. For each rhessys output file ...
variablesList = []
for (i, patchFilepath) in enumerate(patchFilepaths):
    print("\nReading RHESSys output %s from %s  (this may take a while)...\n" \
          % (os.path.basename(patchFilepath), os.path.dirname(patchFilepath)) )
    data = np.genfromtxt(patchFilepath, names=True)
    if len(data) < 1:
        sys.exit("No data found for variable in RHESSys output file '%s'" % \
                 (patchFilepath,) )
    
    if args.year:
        data = data[data['year'] == float(args.year)]
    
    patchIDs = [ int(p) for p in data['patchID'] ]
    expr = VARIABLE_EXPR_RE.sub(r'data["\1"]', args.outputVariable)
    variablesList.append(eval(expr))
        
# Write normalized maps for each input file
for (i, variable) in enumerate(variablesList):
    outputFilePath = outputFilePaths[i]
    # Rescale variable to integer
    variable_scaled = variable * INT_RESCALE
    variable_int = variable_scaled.astype(int)
    # 4. Write reclass rule to temp file
    reclass = open(reclassRule, 'w') 
    for (j, var) in enumerate(variable_int):
        reclass.write("%d:%d:%d:%d\n" % (patchIDs[j], patchIDs[j], var, var) )
    reclass.close()
        
    # 5. Generate map for variable
    print("Mapping variable: {0} ...".format(args.outputVariable))
    result = grassLib.script.run_command('r.recode', 
                                         input=args.patchMap, 
                                         output=RECLASS_MAP_TMP,
                                         rules=reclassRule,
                                         overwrite=True)
    if result != 0:
        sys.exit("Failed to create reclass map for output: {0}".format(outputFilePath) )
    
    # 6. Calculate zonal statistics
    print("Calculating zonal statistics...")
    result = grassLib.script.run_command('r.statistics',
                                         base=args.zones,
                                         cover=RECLASS_MAP_TMP,
                                         method=args.statistic,
                                         output=STATS_MAP_TMP)
    if result != 0:
        sys.exit("Failed to create zonal statisitcs for output: {0}".format(outputFilePath) )
    
    # Keep map (if applicable), re-scaling
    if args.keepmap:
        print("Saving zonal stats to permanent map {0}".format(args.outputFile[i]))
        rMapcalcExpr = '$permrast=@$tmprast/float($scale)'
        grassLib.script.raster.mapcalc(rMapcalcExpr, permrast=args.outputFile[i], tmprast=STATS_MAP_TMP,
                                       scale=INT_RESCALE, verbose=True)
        # Set color table
        result = grassLib.script.run_command('r.colors', 
                                             map=args.outputFile[i],
                                             color='grey1.0')
        if result != 0:
            sys.exit("Failed to modify color map")
        
    
    # 7. Read zonal statistics
    pipe = grassLib.script.pipe_command('r.stats', flags='ln', input=STATS_MAP_TMP)
    stats_scaled = []
    for line in pipe.stdout:
        (parcel, stat) = line.strip().split()
        stats_scaled.append(float(stat))
    stats = np.array(stats_scaled)
    stats = stats / INT_RESCALE
    
    # 8. Make plot
    fig = plt.figure(figsize=(4, 3), dpi=80, tight_layout=True)
    ax1 = fig.add_subplot(111)
    plot_cdf(ax1, stats, xlabel=variableLabel)
    fig.savefig(outputFilePath, bbox_inches='tight', pad_inches=0.125)


# Cleanup
shutil.rmtree(tmpDir)
result = grassLib.script.run_command('g.remove', rast=RECLASS_MAP_TMP)
if result != 0:
    sys.exit("Failed to remove temporary map %s" % (RECLASS_MAP_TMP,) )
result = grassLib.script.run_command('g.remove', rast=STATS_MAP_TMP)
if result != 0:
    sys.exit("Failed to remove temporary map %s" % (STATS_MAP_TMP,) )
