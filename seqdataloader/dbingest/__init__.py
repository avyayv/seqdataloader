## helper functions to ingest bigwig and narrowPeak data files into a tileDB instance.
## tileDB instances are indexed by coordinate
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tiledb
import argparse
import pandas as pd
import numpy as np
from .attrib_config import *
from .utils import *
from ..bounded_process_pool_executor import *
from concurrent import futures 
#from concurrent.futures import ProcessPoolExecutor
#from multiprocessing import Pool 
import pdb
import gc

#graceful shutdown
import psutil
import signal 
import os

def init_worker():
    signal.signal(signal.SIGINT, signal.SIG_IGN)

def kill_child_processes(parent_pid, sig=signal.SIGTERM):
    try:
        parent = psutil.Process(parent_pid)
    except psutil.NoSuchProcess:
        return
    children = parent.children(recursive=True)
    for process in children:
        process.send_signal(sig)
def args_object_from_args_dict(args_dict):
    #create an argparse.Namespace from the dictionary of inputs
    args_object=argparse.Namespace()
    #set the defaults
    vars(args_object)['chrom_threads']=1
    vars(args_object)['task_threads']=1
    vars(args_object)['write_threads']=1
    vars(args_object)['overwrite']=False
    vars(args_object)['batch_size']=1000000
    vars(args_object)['tile_size']=10000
    vars(args_object)['attribute_config']='encode_pipeline'
    vars(args_object)['chunk_size']=10
    for key in args_dict:
        vars(args_object)[key]=args_dict[key]
    #set any defaults that are unset 
    args=args_object    
    return args 


def parse_args():
    parser=argparse.ArgumentParser(description="ingest data into tileDB")
    parser.add_argument("--tiledb_metadata",help="fields are: dataset, fc_bigwig, pval_bigwig, count_bigwig_plus_5p, count_bigwig_minus_5p, idr_peak, overlap_peak, ambig_peak")
    parser.add_argument("--tiledb_group")
    parser.add_argument("--overwrite",default=False,action="store_true") 
    parser.add_argument("--chrom_sizes",help="2 column tsv-separated file. Column 1 = chromsome name; Column 2 = chromosome size")
    parser.add_argument("--chrom_threads",type=int,default=1,help="inner thread pool, launched by task_threads")
    parser.add_argument("--task_threads",type=int,default=1,help="outer thread pool,launched by main thread")
    parser.add_argument("--write_threads",type=int,default=1)
    parser.add_argument("--batch_size",type=int,default=1000000,help="num entries to write at once")
    parser.add_argument("--tile_size",type=int,default=10000,help="tile size")
    parser.add_argument("--attribute_config",default='encode_pipeline',help="the following are supported: encode_pipeline, generic_bigwig")
    parser.add_argument("--chunk_size",type=int,default=10)
    return parser.parse_args()

def create_new_array(size,
                     array_out_name,
                     tile_size,
                     attribute_config,
                     compressor='gzip',
                     compression_level=-1):
    '''
    Creates an empty tileDB array
    '''
    
    tile_size=min(size,tile_size)    
    tiledb_dim = tiledb.Dim(
        name='genome_coordinate',
        domain=(0, size - 1),
        tile=tile_size,
        dtype='uint32')
    #config
    tdb_Config=tiledb.Config({"sm.check_coord_dups":"false",
                              "sm.check_coord_oob":"false",
                              "sm.check_global_order":"false",
                              "sm.num_writer_threads":50,
                              "sm.num_reader_threads":50})
    tdb_Context=tiledb.Ctx(config=tdb_Config) 
    tiledb_dom = tiledb.Domain(tiledb_dim,ctx=tdb_Context)

    #generate the attribute information
    attribute_info=get_attribute_info(attribute_config)
    attribs=[]
    for key in attribute_info:
        attribs.append(tiledb.Attr(
            name=key,
            filters=tiledb.FilterList([tiledb.GzipFilter()]),
            dtype=attribute_info[key]['dtype']))
    tiledb_schema = tiledb.ArraySchema(
        domain=tiledb_dom,
        attrs=tuple(attribs),
        cell_order='row-major',
        tile_order='row-major')
    
    tiledb.DenseArray.create(array_out_name, tiledb_schema)
    print("created empty array on disk")
    del tdb_Config
    del tdb_Context
    del tiledb_dom 
    gc.collect() 
    return

def write_chunk(inputs):
    array_out_name=inputs[0]
    start=inputs[1]
    end=inputs[2]
    sub_dict=inputs[3]
    batch_size=inputs[4]
    with tiledb.DenseArray(array_out_name,ctx=tiledb.Ctx(),mode='w') as out_array:
        out_array[start:end]=sub_dict
        print("done with chunk start:"+str(start)+", end:"+str(end))
    del sub_dict
    gc.collect() 
    return "done"
    
def write_array_to_tiledb(size,
                          attribute_config,
                          dict_to_write,
                          array_out_name,
                          batch_size=10000,
                          compressor='gzip',
                          compression_level=-1,
                          updating=False,
                          write_threads=1):
    print("starting to write output")
    try:
        if updating is True:
            #we are only updating some attributes in the array
            with tiledb.DenseArray(array_out_name,mode='r',ctx=tiledb.Ctx()) as cur_array:
                cur_vals=cur_array[:]
            print('got cur vals') 
            for key in dict_to_write:
                cur_vals[key]=dict_to_write[key]
            dict_to_write=cur_vals
            print("updated data dict for writing") 
        else:
            #we are writing for the first time, make sure all attributes are provided, if some are not, use a nan array
            required_attrib=list(get_attribute_info(attribute_config).keys())
            for attrib in required_attrib:
                if attrib not in dict_to_write:
                    dict_to_write[attrib]=np.full(size,np.nan)
        print("finalizing the write")
        dict_keys=list(dict_to_write.keys())
        num_entries=len(dict_to_write[dict_keys[0]])
        pool_inputs=[]
        for i in range(0,num_entries,batch_size):
            subdict={}
            for key in dict_keys:
                subdict[key]=dict_to_write[key][i:i+batch_size]
            pool_inputs.append((array_out_name,i,i+batch_size,subdict,batch_size))
        subdict={}
        for key in dict_keys:
            subdict[key]=dict_to_write[key][i+batch_size::]
        pool_inputs.append((array_out_name,i+batch_size,num_entries,subdict,batch_size))
        with BoundedProcessPoolExecutor(max_workers=write_threads,initializer=init_worker) as pool:
            print("made pool")
            for chunk in chunkify(pool_inputs,chunk=chunk_size):
                future_to_element=dict()
                for element in chunk:
                    future=pool.submit(write_chunk,element)
                    future_to_element[future]=element
                for future in futures.as_completed(future_to_element):
                    elt=future_to_element[future]
                    del elt
                    gc.collect() 
        print("done writing")
        del pool_inputs
        del dict_to_write
        del subdict
        gc.collect()

    except KeyboardInterrupt:
        print('detected keyboard interrupt')
        #shutdown the pool
        pool.shutdown(wait=False)
        # Kill remaining child processes
        kill_child_processes(os.getpid())
        raise 
    except Exception as e:
        print(repr(e))
        #shutdown the pool
        pool.shudown(wait=False)
        # Kill remaining child processes
        kill_child_processes(os.getpid())
        raise e
    
def extract_metadata_field(row,field):
    dataset=row['dataset'] 
    try:
        return row[field]
    except:
        print("tiledb_metadata has no column "+field+" for dataset:"+str(dataset))
        return None

def open_data_for_parsing(row,attribute_info):
    try:
        data_dict={}
        cols=list(row.index)
        if 'dataset' in cols:
            cols.remove('dataset')
        for col in cols:
            cur_fname=extract_metadata_field(row,col)
            if cur_fname is not None:
                data_dict[col]=attribute_info[col]['opener'](cur_fname)
        return data_dict
    except Exception as e:
        print(repr(e))
        raise e

def process_chrom(inputs):
    chrom=inputs[0]
    size=inputs[1]
    array_out_name=inputs[2]
    data_dict=inputs[3]
    attribute_info=inputs[4]
    args=inputs[5]
    overwrite=args.overwrite
    write_threads=args.write_threads
    batch_size=args.batch_size
    tile_size=args.tile_size
    attribute_config=args.attribute_config
    updating=False
    if tiledb.object_type(array_out_name) == "array":
        if overwrite==False:
            raise Exception("array:"+str(array_out_name) + "already exists; use the --overwrite flag to overwrite it. Exiting")
        else:
            print("warning: the array: "+str(array_out_name)+" already exists. You provided the --overwrite flag, so it will be updated/overwritten")
            updating=True
    else:
        #create the array:
        create_new_array(size=size,
                         attribute_config=attribute_config,
                         array_out_name=array_out_name,
                         tile_size=tile_size)
        print("created new array:"+str(array_out_name))
    dict_to_write=dict()
    for attribute in data_dict:
        store_summits=False
        summit_indicator=None
        if 'store_summits' in attribute_info[attribute]:
            store_summits=attribute_info[attribute]['store_summits']
        print("store_summits:"+str(store_summits))
        if 'summit_indicator' in attribute_info[attribute]: 
            summit_indicator=attribute_info[attribute]['summit_indicator']
        print("summit_indicator:"+str(summit_indicator))
        dict_to_write[attribute]=attribute_info[attribute]['parser'](data_dict[attribute],chrom,size,store_summits,summit_indicator)
        print("got:"+str(attribute)+" for chrom:"+str(chrom))
    
    write_array_to_tiledb(size=size,
                          attribute_config=attribute_config,
                          dict_to_write=dict_to_write,
                          array_out_name=array_out_name,
                          updating=updating,
                          batch_size=batch_size,
                          write_threads=write_threads)
    print("wrote array to disk for dataset:"+str(array_out_name))
    del dict_to_write
    del pool_inputs
    gc.collect() 
    return 'done'

def create_tiledb_array(inputs):
    '''
    create new tileDB array for a single dataset, overwrite if array exists and user sets --overwrite flag
    '''
    try:
        row=inputs[0]
        args=inputs[1]
        chrom_sizes=inputs[2]
        attribute_info=inputs[3]
        
        #get the current dataset
        dataset=row['dataset']    
        #read in filenames for bigwigs
        data_dict=open_data_for_parsing(row,attribute_info)
        pool_inputs=[] 
        array_outf_prefix="/".join([args.tiledb_group,dataset])
        print("parsed pool inputs") 
        for index, row in chrom_sizes.iterrows():
            chrom=row[0]
            size=row[1]
            array_out_name='.'.join([array_outf_prefix,chrom])
            pool_inputs.append((chrom,size,array_out_name,data_dict,attribute_info,args))
        with BoundedProcessPoolExecutor(max_workers=args.chrom_threads,initializer=init_worker) as pool:
            print("made pool!")
            for chunk in chunkify(pool_inputs,chunk=chunk_size):
                future_to_element=dict()
                for element in chunk:
                    future=pool.submit(process_chrom,element)
                    future_to_element[future]=element
                for future in futures.as_completed(future_to_element):
                    elt=future_to_element[future]
                    del elt
        del pool_inputs
        del data_dict
        gc.collect()
        
    except KeyboardInterrupt:
        print('detected keyboard interrupt')
        #shutdown the pool
        pool.shutdown(wait=False)
        # Kill remaining child processes
        kill_child_processes(os.getpid())
        raise 
    except Exception as e:
        print(repr(e))
        #shutdown the pool
        pool.shutdown(wait=False)
        # Kill remaining child processes
        kill_child_processes(os.getpid())
        raise e
    return "done"

    
def ingest(args):
    if type(args)==type({}):
        args=args_object_from_args_dict(args)
    global chunk_size
    chunk_size=args.chunk_size
    
    attribute_info=get_attribute_info(args.attribute_config) 
    tiledb_metadata=pd.read_csv(args.tiledb_metadata,header=0,sep='\t')
    
    print("loaded tiledb metadata")
    chrom_sizes=pd.read_csv(args.chrom_sizes,header=None,sep='\t')
    print("loaded chrom sizes")
    pool_inputs=[]
    
    #check if the tiledb_group exists, and if not, create it
    if tiledb.object_type(args.tiledb_group) is not 'group':        
        group_uri=tiledb.group_create(args.tiledb_group)
        print("created tiledb group") 
    else:
        group_uri=args.tiledb_group
        print("tiledb group already exists")
        
    for index,row in tiledb_metadata.iterrows():
        pool_inputs.append([row,args,chrom_sizes,attribute_info])
    try:
        #iterate through the tasks 
        with BoundedProcessPoolExecutor(max_workers=args.task_threads,initializer=init_worker) as pool:
            for chunk in chunkify(pool_inputs,chunk=chunk_size):
                future_to_element=dict()
                for element in chunk:
                    future=pool.submit(create_tiledb_array,element)
                    future_to_element[future]=element
                for future in futures.as_completed(future_to_element):
                    elt=future_to_element[future]
                    del elt
    except KeyboardInterrupt:
        print("keyboard interrupt detected")
        #shutdown the pool
        pool.shutdown(wait=False)
        # Kill remaining child processes
        kill_child_processes(os.getpid())
        raise 
    except Exception as e:
        print(repr(e))
        #shutdown the pool
        pool.shutdown(wait=False)
        # Kill remaining child processes
        kill_child_processes(os.getpid())
        raise e
    return "done"

def main():
    args=parse_args()
    ingest(args)
    
    
if __name__=="__main__":
    main() 
    
    
