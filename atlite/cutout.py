## Copyright 2016-2017 Gorm Andresen (Aarhus University), Jonas Hoersch (FIAS), Tom Brown (FIAS)

## This program is free software; you can redistribute it and/or
## modify it under the terms of the GNU General Public License as
## published by the Free Software Foundation; either version 3 of the
## License, or (at your option) any later version.

## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.

## You should have received a copy of the GNU General Public License
## along with this program.  If not, see <http://www.gnu.org/licenses/>.


"""
Renewable Energy Atlas Lite (Atlite)

Light-weight version of Aarhus RE Atlas for converting weather data to power systems data
"""

from __future__ import absolute_import

import xarray as xr
import pandas as pd
import numpy as np
import os, sys, shutil
import filelock
from six import itervalues
from multiprocessing import Pool

import logging
logger = logging.getLogger(__name__)

from . import ncep, cordex
from .convert import heat_demand, wind
from .aggregate import aggregate_sum, aggregate_matrix
from .config import weather_dataset, cutout_dir

def cutout_preparation_do_task(task, write_to_file=True):
    task = task.copy()
    prepare_func = task.pop('prepare_func')
    if write_to_file:
        datasetfns = task.pop('datasetfns')

    try:
        data = prepare_func(**task)
        if data is None:
            data = []

        if write_to_file:
            for yearmonth, ds in data:
                fn = datasetfns[yearmonth]
                ds = ds.load() # Don't loose time waiting for the lock, but increases the mem consumption to just about 2gb

                if write_to_file:
                    with filelock.SoftFileLock(fn + '.lock'):
                        ds.to_netcdf(fn, mode='a')
                    logger.debug("Appended variable(s) %s to %s generated by %s",
                                 ", ".join('`' + x + '`' for x in ds.data_vars),
                                 os.path.basename(fn),
                                 prepare_func.__name__)
    except Exception as e:
        logger.exception("Exception occured in the task with prepare_func `%s`: %s",
                         prepare_func.__name__, e.args[0])
        raise e

    if not write_to_file:
        return data

class Cutout(object):
    def __init__(self, name=None, nprocesses=None,
                 weather_dataset=weather_dataset, cutout_dir=cutout_dir,
                 **cutoutparams):
        self.name = name
        self.nprocesses = nprocesses

        weather_dataset_m = sys.modules['atlite.' + weather_dataset]
        self.weather_data_config = weather_dataset_m.weather_data_config.copy()
        self.meta_data_config = weather_dataset_m.meta_data_config.copy()

        self.cutout_dir = os.path.join(cutout_dir, name)
        self.prepared = False
        if os.path.isdir(self.cutout_dir):
	    if weather_dataset is 'ncep':
                self.meta = meta = xr.open_dataset(self.datasetfn()).stack(**{'year-month': ('year', 'month')})
                # check datasets very rudimentarily, series and coordinates should be checked as well
                if all(os.path.isfile(self.datasetfn(ym)) for ym in meta.coords['year-month'].to_index()):
                    self.prepared = True
                else:
                    assert False
	    elif weather_dataset is 'cordex':
                self.meta = meta = xr.open_dataset(self.datasetfn())
		meta_year = self.coords['year']
		self.meta = meta = meta.stack(**{'year-month': ('year', 'month')})
                # check datasets very rudimentarily, series and coordinates should be checked as well
                if all(os.path.isfile(self.datasetfn(ym)) for ym in meta_year.coords['year'].to_index()):
                    self.prepared = True
                else:
                    assert False
	    else:
                raise NameError("'weather_dataset' needs to be specified as 'ncep' or 'cordex'")

        if not self.prepared:
            if {"lons", "lats", "years"}.difference(cutoutparams):
                raise TypeError("Arguments `lons`, `lats` and `years` need to be specified")
            self.meta = self.get_meta(**cutoutparams)

    def datasetfn(self, *args):
	dataset = None
	if weather_dataset is 'ncep':
	    if len(args) == 2:
            	dataset = args
            elif len(args) == 1:
            	dataset = args[0]
       	    else:
            	dataset = None
            setting = os.path.join(self.cutout_dir, "meta.nc"
                                            	    	if dataset is None
                                            	    	else "{}{:0>2}.nc".format(*dataset))
        elif weather_dataset is 'cordex':
	    if len(args) == 2:
            	dataset = args
            elif len(args) == 1:
            	dataset = args
            elif args == None:
            	dataset = None
	    setting = os.path.join(self.cutout_dir, "meta.nc"
                                            	    	if dataset is None
                                            	    	else "{}.nc".format(*dataset))
        else:
            raise NameError("'weather_dataset' needs to be specified as 'ncep' or 'cordex'")

        return setting

    @property
    def coords(self):
        return self.meta.coords

    def get_meta(self, lons, lats, years, months=None):
        if months is None:
            months = slice(1, 12)
        meta_kwds = self.meta_data_config.copy()
        prepare_func = meta_kwds.pop('prepare_func')
        ds = prepare_func(lons=lons, lats=lats, year=years.stop, month=months.stop, **meta_kwds)

        offset_start = (pd.Timestamp(ds.coords['time'].values[0]) -
                        pd.Timestamp("{}-{}".format(years.stop, months.stop)))
        offset_end = (pd.Timestamp(ds.coords['time'].values[-1]) -
                      (pd.Timestamp("{}-{}".format(years.stop, months.stop)) +
                       pd.offsets.MonthBegin()))

	if weather_dataset is 'ncep':
            freq = 'h'
        elif weather_dataset is 'cordex':
            freq = '3h'
        else:
            raise NameError("'weather_dataset' needs to be specified as 'ncep' or 'cordex'")

        ds.coords["time"] = pd.date_range(
            start=pd.Timestamp("{}-{}".format(years.start, months.start)) + offset_start,
            end=(pd.Timestamp("{}-{}".format(years.stop, months.stop))
                 + pd.offsets.MonthBegin() + offset_end),
            freq=freq)

        ds.coords["year"] = range(years.start, years.stop+1)
        ds.coords["month"] = range(months.start, months.stop+1)
        ds = ds.stack(**{'year-month': ('year', 'month')})

        ds.coords["year"] = range(years.start, years.stop+1)

        return ds

    @property
    def shape(self):
        return len(self.coords["lon"]), len(self.coords["lat"])

    def grid_coordinates(self, latlon=False):
        lats, lons = np.meshgrid(self.coords["lat"], self.coords["lon"])
        if latlon:
            return np.asarray((np.ravel(lats), np.ravel(lons))).T
        else:
            return np.asarray((np.ravel(lons), np.ravel(lats))).T

    def grid_cells(self):
        from shapely.geometry import box
        coords = self.grid_coordinates()
        span = (coords[self.shape[1]+1] - coords[0]) / 2
        return [box(*c) for c in np.hstack((coords - span, coords + span))]

    @property
    def extent(self):
        return (list(self.coords["lon"].values[[0, -1]]) +
                list(self.coords["lat"].values[[-1, 0]]))

    def __repr__(self):
        yearmonths = self.coords['year-month'].to_index()
        return ('<Cutout {} lon={:.2f}-{:.2f} lat={:.2f}-{:.2f} time={}/{}-{}/{} {}prepared>'
                .format(self.name,
                        self.coords['lon'].values[0], self.coords['lon'].values[-1],
                        self.coords['lat'].values[0], self.coords['lat'].values[-1],
                        yearmonths[0][0],  yearmonths[0][1],
                        yearmonths[-1][0], yearmonths[-1][1],
                        "" if self.prepared else "UN"))

    def prepare(self, overwrite=False):
        if self.prepared and not overwrite:
            raise ArgumentError("The cutout is already prepared. If you want to recalculate it, "
                                "anyway, then you must supply an `overwrite=True` argument.")

        logger.info("Starting preparation of cutout '%s'", self.name)

        cutout_dir = self.cutout_dir
        yearmonths = self.coords['year-month'].to_index()
	years = self.coords['year'].to_index()
        lons = self.coords['lon']
        lats = self.coords['lat']

        if weather_dataset is 'ncep':
            yearmonths = yearmonths
        elif weather_dataset is 'cordex':
            yearmonths = years
        else:
            raise NameError("weather_dataset need to be specified as 'ncep' or 'cordex'")

        # Delete cutout_dir
        if os.path.isdir(cutout_dir):
            logger.debug("Deleting cutout_dir '%s'", cutout_dir)
            shutil.rmtree(cutout_dir)

        logger.debug("Creating empty netcdf files for all months in '%s'", cutout_dir)
        # Create all datasets beforehand
        datasetfns = {ym: self.datasetfn(ym) for ym in [None] + yearmonths.tolist()}
        os.mkdir(cutout_dir)
        self.meta.unstack('year-month').to_netcdf(datasetfns[None])
        for ym in yearmonths:
            xr.Dataset().to_netcdf(datasetfns[ym])

        # Compute data and fill files
        tasks = []
        for series in itervalues(self.weather_data_config):
            series = series.copy()
            tasks_func = series.pop('tasks_func')
            tasks += tasks_func(lons=lons, lats=lats, yearmonths=yearmonths, **series)
        for t in tasks:
            t['datasetfns'] = datasetfns

        logger.info("%d tasks have been collected. Starting running them on %s.",
                    len(tasks),
                    ("%d processes" % self.nprocesses)
                    if self.nprocesses is not None
                    else "all processors")

        pool = Pool(processes=self.nprocesses)
        try:
            pool.map(cutout_preparation_do_task, tasks)
        except Exception as e:
            pool.terminate()
            logger.info("Preparation of cutout '%s' has been interrupted by an exception. "
                        "Purging the incomplete cutout_dir.",
                        self.name)
            shutil.rmtree(cutout_dir)
            raise e
        pool.close()
        logger.info("Cutout '%s' has been successfully prepared", self.name)
        self.prepared = True

    def produce_specific_dataseries(self, yearmonth, series_name):
        lons = self.coords['lon']
        lats = self.coords['lat']
        series = self.weather_data_config[series_name].copy()
        tasks_func = series.pop('tasks_func')
        tasks = tasks_func(lons=lons, lats=lats, yearmonths=[yearmonth], **series)
        assert len(tasks) == 1
        data = cutout_preparation_do_task(tasks[0], write_to_file=False)
        assert len(data) == 1 and data[0][0] == yearmonth
        return data[0][1]

    def convert_and_aggregate(self, convert_func, matrix=None, index=None, **convert_kwds):
        assert self.prepared, "The cutout has to be prepared first."

        if matrix is not None:
            if index is None:
                index = pd.RangeIndex(matrix.shape[0])
            aggregate_func = aggregate_matrix
            aggregate_kwds = dict(matrix=matrix, index=index)
        else:
            aggregate_func = aggregate_sum
            aggregate_kwds = {}

        results = []
        for ym in self.coords['year-month'].to_index():
            with xr.open_dataset(self.datasetfn(ym)) as ds:
                da = convert_func(ds, **convert_kwds).load()
            results.append(aggregate_func(da, **aggregate_kwds))
        if 'time' in results[0]:
            results = xr.concat(results, dim='time')
        else:
            results = sum(results)
        return results

    ## Conversion and aggregation functions

    heat_demand = heat_demand

    wind = wind
