#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (c) 2014, 2015 Adam.Dybbroe

# Author(s):

#   Adam.Dybbroe <a000680@c14526.ad.smhi.se>
#   Panu Lahtinen <panu.lahtinen@fmi.fi>

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""PPS netcdf cloud product reader
"""

import os.path
from ConfigParser import ConfigParser
from ConfigParser import NoOptionError
from glob import glob

from datetime import datetime, timedelta
import numpy as np
import h5py
from trollsift import Parser

TEPSILON = timedelta(seconds=1)

import mpop.channel
from mpop import CONFIG_PATH
from mpop.plugin_base import Reader

import logging
LOG = logging.getLogger(__name__)


class InconsistentDataDimensions(Exception):
    pass


def unzip_file(filename):
    """Unzip the file if file is bzipped = ending with 'bz2'"""

    import tempfile
    import bz2
    if filename.endswith('bz2'):
        bz2file = bz2.BZ2File(filename)
        tmpfilename = tempfile.mktemp()
        try:
            ofpt = open(tmpfilename, 'wb')
            ofpt.write(bz2file.read())
            ofpt.close()
        except IOError:
            import traceback
            traceback.print_exc()
            LOG.info("Failed to read bzipped file %s", str(filename))
            os.remove(tmpfilename)
            return None

        return tmpfilename

    return None

#
#
# http://yloiseau.net/articles/DesignPatterns/flyweight/


class GeolocationFlyweight(object):

    def __init__(self, cls):
        self._cls = cls
        self._instances = dict()

    def __call__(self, *args, **kargs):
        """
        we assume that this is only used for the gelocation object,
        filenames are listed in the second argument

        """
        return self._instances.setdefault(tuple(args[1]),
                                          self._cls(*args, **kargs))

    def clear_cache(self):
        del self._instances
        self._instances = dict()


#@GeolocationFlyweight
class PpsGeolocationData(object):

    def __init__(self, shape, granule_lengths, filenames):
        self.filenames = filenames
        self.shape = shape
        self.granule_lengths = granule_lengths
        self.longitudes = None
        self.latitudes = None
        self.row_indices = None
        self.col_indices = None
        self.mask = None

    def read(self):
        """
        Read longitudes and latitudes from geo filenames and assemble
        """

        if self.longitudes is not None:
            return self

        self.longitudes = np.empty(self.shape,
                                   dtype=np.float32)
        self.latitudes = np.empty(self.shape,
                                  dtype=np.float32)
        self.mask = np.zeros(self.shape,
                             dtype=np.bool)

        swath_index = 0
        for idx, filename in enumerate(self.filenames):

            y0_ = swath_index
            y1_ = swath_index + self.granule_lengths[idx]
            swath_index = swath_index + self.granule_lengths[idx]

            get_lonlat_into(filename,
                            self.longitudes[y0_:y1_, :],
                            self.latitudes[y0_:y1_, :],
                            self.mask[y0_:y1_, :])

        self.longitudes = np.ma.array(self.longitudes,
                                      mask=self.mask,
                                      copy=False)
        self.latitudes = np.ma.array(self.latitudes,
                                     mask=self.mask,
                                     copy=False)

        LOG.debug("Geolocation read in for... " + str(self))
        return self


class HDF5MetaData(object):

    """

    Small class for inspecting a HDF5 file and retrieve its metadata/header
    data. It is developed for JPSS/NPP data but is really generic and should
    work on most other hdf5 files.

    Supports

    """

    def __init__(self, filename):
        self.metadata = {}
        self.filename = filename
        if not os.path.exists(filename):
            raise IOError("File %s does not exist!" % filename)

    def read(self):

        filename = self.filename
        unzipped = unzip_file(filename)
        if unzipped:
            filename = unzipped

        with h5py.File(filename, 'r') as h5f:
            h5f.visititems(self.collect_metadata)
            self._collect_attrs('/', h5f.attrs)

        if unzipped:
            os.remove(unzipped)

        return self

    def _collect_attrs(self, name, attrs):
        for key, value in attrs.iteritems():
            # Throws a TypeError if key=DIMESNION_LIST
            # Observed at FMI (Panu) - maybe hdf5 specific?
            # FIXME!
            if key in ['DIMENSION_LIST']:
                continue
            value = np.squeeze(value)
            if issubclass(value.dtype.type, str):
                self.metadata["%s/attr/%s" % (name, key)] = str(value)
            else:
                self.metadata["%s/attr/%s" % (name, key)] = value

    def collect_metadata(self, name, obj):
        if isinstance(obj, h5py.Dataset):
            self.metadata["%s/shape" % name] = obj.shape
        self._collect_attrs(name, obj.attrs)

    def __getitem__(self, key):

        long_key = None
        for mkey in self.metadata.keys():
            if mkey.endswith(key):
                if long_key is not None:
                    raise KeyError("Multiple keys called %s" % key)
                long_key = mkey
                break
        return self.metadata[long_key]

    def keys(self):
        return self.metadata.keys()

    def get_data_keys(self):

        data_keys = []
        for key in self.metadata.keys():
            if key.endswith("/shape"):
                data_key = key.split("/shape")[0]
                data_keys.append(data_key)
        return data_keys

    def get_data_keys_and_shapes(self):

        data_keys = {}
        for key in self.metadata.keys():
            if key.endswith("/shape"):
                data_key = key.split("/shape")[0]
                shape = self.metadata[key]
                data_keys[data_key] = shape

        return data_keys


class PPSMetaData(HDF5MetaData):

    def get_shape(self):

        nx = 0
        ny = 0
        for key in self.metadata:
            if key.endswith('nx/shape'):
                nx = self.metadata[key][0]
            if key.endswith('ny/shape'):
                ny = self.metadata[key][0]

        return nx, ny

    def get_header_info(self):

        info = {}
        for key in self.metadata:
            if key.endswith('platform'):
                info['platform_name'] = self.metadata[key]
            elif key.endswith('orbit_number'):
                info['orbit'] = self.metadata[key]
            elif key.endswith('time_coverage_start'):
                info['time_slot'] = datetime.strptime(self.metadata[key][:-2],
                                                      "%Y%m%dT%H%M%S")
        return info

    def get_dataset_attributes(self, var_name):

        retv = {}
        for key in self.metadata:
            if key.split('/')[0] == var_name and key.find('attr') > 0:
                dictkey = key.split('/')[-1]
                if dictkey in ['DIMENSION_LIST']:
                    continue
                # LOG.debug("var_name, full-key, key, value: %s %s %s %s",
                #          var_name, key, dictkey, self.metadata[key])
                retv[dictkey] = self.metadata[key]

        return retv

    def get_root_attributes(self):

        retv = {}
        for key in self.metadata:
            if key.startswith('//attr'):
                dictkey = key.split('/')[-1]
                retv[dictkey] = self.metadata[key]

        return retv


def get_filenames(scene, products, conf, starttime, endtime, area_name):
    """Get list of filenames within time interval"""

    filename = conf.get(scene.instrument_name + "-level3",
                        "cloud_product_filename",
                        raw=True,
                        vars=os.environ)
    directory = conf.get(scene.instrument_name + "-level3",
                         "cloud_product_dir",
                         vars=os.environ)
    pathname_tmpl = os.path.join(directory, filename)

    if not scene.orbit:
        orbit = ""
    else:
        orbit = scene.orbit

    flist_allproducts = []
    for product in products:
        values = {"area": area_name,
                  "satellite": scene.satname + scene.number,
                  "product": product}

        if endtime:
            # Okay, we need to check for more than one granule/swath!
            # First get all files with all times matching in directory:
            values["orbit"] = '?????'
            filename_tmpl = os.path.join(
                directory, globify(filename)) % values

        else:
            values["orbit"] = str(orbit).zfill(5) or "*"
            filename_tmpl = scene.time_slot.strftime(
                pathname_tmpl) % values

        LOG.debug("File path = %s", str(filename_tmpl))
        file_list = glob(filename_tmpl)
        if len(file_list) == 0:
            LOG.warning("No %s product matching", str(product))
        elif len(file_list) > 1 and not endtime:
            LOG.warning("More than 1 file matching for %s: %s",
                        str(product), str(file_list))
            file_list = []
        elif len(file_list) > 1:
            file_list = extract_filenames_in_time_window(
                file_list, starttime, endtime)

        if len(file_list) == 0:
            LOG.warning(
                "No files found matching time window for product %s", product)

        flist_allproducts = flist_allproducts + file_list

    return flist_allproducts


def extract_filenames_in_time_window(file_list, starttime, endtime):
    """Extract the filenames with time inside the time interval specified.
    NB! Only supports EARS-NWC granules. This does not support assembling 
    several locally received full swaths"""

    # New filenames:
    # Ex.:
    # W_XX-EUMETSAT-Darmstadt,SING+LEV+SAT,NOAA19+CT_C_EUMS_20150819124700_33643.nc.bz2
    pnew = Parser(
        "W_XX-EUMETSAT-Darmstadt,SING+LEV+SAT,{platform_name:s}+{product:s}_C_EUMS_{starttime:%Y%m%d%H%M}00_{orbit:05d}")
    # Old filenames:
    # Ex.:
    # ctth_20130910_205300_metopb.h5.bz2
    pold = Parser(
        "{product:s}_{starttime:%Y%m%d_%H%M}00_{platform_name:s}")

    valid_filenames = []
    valid_times = []
    LOG.debug("Time Window: (%s, %s)", str(starttime), str(endtime))
    for fname in file_list:
        try:
            data = pnew.parse(os.path.basename(fname).split('.nc')[0])
        except ValueError:
            data = pold.parse(os.path.basename(fname).split('.h5')[0])

        if (data['starttime'] >= starttime and
                data['starttime'] < endtime):
            valid_filenames.append(fname)
            valid_times.append(data['starttime'])
            LOG.debug("Start time %s inside window", str(data['starttime']))
        else:
            # LOG.debug("Start time: %s outside (%s, %s)",
            #          str(data['starttime']), str(starttime), str(endtime))
            pass

    # Can we rely on the files being sorted according to time?
    # Sort the filenames according to time:
    vtimes = np.array(valid_times)
    idx = np.argsort(vtimes)
    vfiles = np.array(valid_filenames)
    return np.take(vfiles, idx).tolist()


class InfoObject(object):

    """Simple data and info container.
    """

    def __init__(self):
        self.info = {}
        self.data = None


class NwcSafPpsChannel(mpop.channel.GenericChannel):

    def __init__(self):
        mpop.channel.GenericChannel.__init__(self)
        self.mda = {}
        self._projectables = []
        self.shape = None
        self.granule_lengths = None
        self.filenames = None

        self.platform_name = None
        self.begin_time = None
        self.end_time = None
        self.orbit_begin = None
        self.orbit_end = None

    def read(self, pps_product):
        """Read the PPS v2014 formatet data"""

        LOG.debug("Read the PPS product data...")

        self._projectables = pps_product.projectables
        self.granule_lengths = pps_product.granule_lengths
        self.shape = pps_product.shape
        self.filenames = pps_product.filenames
        self.orbit_begin = pps_product.orbit_begin
        self.orbit_end = pps_product.orbit_end
        self.platform_name = pps_product.platform_name
        self.begin_time = pps_product.begin_time
        self.end_time = pps_product.end_time

        # Take the metadata of the first granule and store as global
        #self.mda = pps_product.metadata[0].metadata
        mda = pps_product.metadata[0]
        self.mda = mda.metadata
        self.mda.update(mda.get_root_attributes())

        for var_name in self._projectables:
            setattr(self, var_name, InfoObject())
            # Fill the info dict...
            getattr(self, var_name).info = mda.get_dataset_attributes(var_name)
            getattr(self, var_name).data = np.ma.masked_array(pps_product.raw_data[var_name],
                                                              mask=pps_product.mask[var_name])

        return

    def project(self, coverage):
        """Project the data"""
        LOG.debug("Projecting channel %s...", self.name)
        import copy
        res = copy.copy(self)

        # Project the data
        for var in self._projectables:
            LOG.info("Projecting %s", str(var))
            res.__dict__[var] = copy.copy(self.__dict__[var])
            res.__dict__[var].data = coverage.project_array(
                self.__dict__[var].data)

        res.name = self.name
        res.resolution = self.resolution
        res.filled = True
        res.area = coverage.out_area
        return res

    def is_loaded(self):
        """Tells if the channel contains loaded data.
        """
        return True
        # return len(self._projectables) > 0

    def save(self, filename, old=True, **kwargs):
        """Save to old format"""
        del kwargs
        if old:
            try:
                from nwcsaf_formats.ppsv2014_to_oldformat import write_product
                write_product(self, filename)
            except ImportError:
                LOG.error("Could not save to old format")
                raise
        else:
            raise NotImplementedError("Can't save to new pps format yet.")


class PPSProductData(object):

    """Placeholder for the PPS netCDF product data. Reads the
    netCDF files using h5py. One file for each product and granule/swath.

    """

    def __init__(self, filenames):

        self.filenames = filenames
        self.mda = {}
        self.projectables = []
        self._keys = []
        self._refs = {}
        self.shape = None
        self.product_name = 'unknown'
        self.platform_name = None

        self.begin_time = None
        self.end_time = None
        self.orbit_begin = None
        self.orbit_end = None

        self.metadata = []

        self.raw_data = {}
        self.mask = {}

        self.granule_lengths = []

    def read(self):
        """Read the PPS v2014 formatet data"""

        self._read_metadata()

        for key in self.raw_data:
            LOG.debug("Shape of data: %s", str(self.raw_data[key].shape))
            break

        self._read_data()

        return self

    def _read_metadata(self):

        LOG.debug("Filenames: %s", str(self.filenames))

        swath_length = 0
        swath_width = None
        for fname in self.filenames:
            LOG.debug("Get and append metadata from file: " + str(fname))
            md = PPSMetaData(fname).read()
            # Set the product_name variable:
            try:
                self.product_name = md['product_name']
            except KeyError:
                LOG.warning("No product_name in file!")

            width, granule_length = md.get_shape()
            hdd = md.get_header_info()
            if not self.platform_name and 'platform_name' in hdd:
                self.platform_name = hdd['platform_name']
            if not self.begin_time and 'time_slot' in hdd:
                self.begin_time = hdd['time_slot']
            if 'time_slot' in hdd:
                self.end_time = hdd['time_slot']
            if not self.orbit_begin and 'orbit' in hdd:
                self.orbit_begin = int(hdd['orbit'])
            if 'orbit' in hdd:
                self.orbit_end = int(hdd['orbit'])

            self.metadata.append(md)
            self.granule_lengths.append(granule_length)

            if swath_width:
                if swath_width != width:
                    raise InconsistentDataDimensions('swath_width not the same ' +
                                                     'between granules: %d %d',
                                                     swath_width, width)

            swath_width = width
            swath_length = swath_length + granule_length

        # Take the first granule, and find what data fields it contains
        # and assume all granules have those same data fields:
        md = self.metadata[0]
        dks = md.get_data_keys_and_shapes()

        for key in dks:
            if key in ['lon', 'lat', 'lat_reduced', 'lon_reduced']:
                LOG.debug("Key = %s", str(key))
                continue
            shape = dks[key]
            if len(shape) == 3 and shape[0] == 1:
                shape = shape[1], shape[2]
            if shape == (self.granule_lengths[0], swath_width):
                self.projectables.append(key)
            else:
                self.mda.update({key: dks[key]})

            LOG.debug("Key, shape, granule_length, swath_width: %s %s %s %s",
                      str(key), str(shape),
                      str(self.granule_lengths[0]), str(swath_width))

        # initiate data arrays
        self.shape = swath_length, swath_width

        for field in self.projectables:
            dtype = md[field + '/attr/valid_range'].dtype
            self.raw_data[str(field)] = np.zeros(self.shape, dtype=dtype)
            self.mask[field] = np.zeros(self.shape, dtype=np.bool)

    def _read_data(self):
        """Loop over all granules and read one granule at a time and 
        fill the data arrays"""

        LOG.debug("Read all %s product files...", self.product_name)
        swath_index = 0
        for idx, md in enumerate(self.metadata):
            filename = self.filenames[idx]

            unzipped = unzip_file(filename)
            if unzipped:
                filename = unzipped

            h5f = h5py.File(filename, 'r')

            variables = {}
            for key, item in h5f.items():
                if item.attrs.get("CLASS") != 'DIMENSION_SCALE':
                    variables[key] = item

            # processed variables
            processed = set()
            non_processed = set(variables.keys()) - processed

            fields = {}
            for var_name in non_processed:
                if var_name in ['lon', 'lat', 'lon_reduced', 'lat_reduced']:
                    continue

                var = variables[var_name]
                if ("standard_name" not in var.attrs.keys() and
                        "long_name" not in var.attrs.keys()):
                    LOG.warning("Data field %s is lacking both standard_name and long_name",
                                var_name)
                    continue

                if var_name not in self.projectables:
                    self.metadata[idx].metadata[var_name] = var[:]
                    continue

                data = var[:]
                if len(data.shape) == 3 and data.shape[0] == 1:
                    #LOG.debug("Rip off the first dimension of length 1")
                    data = data[0]

                if 'valid_range' in var.attrs.keys():
                    data = np.ma.masked_outside(
                        data, *var.attrs['valid_range'])
                elif '_FillValue' in var.attrs.keys():
                    data = np.ma.masked_where(data, var.attrs['_FillValue'])
                if "scale_factor" in var.attrs.keys() and \
                   "add_offset" in var.attrs.keys():
                    dataset = (data * var.attrs.get("scale_factor", 1)
                               + var.attrs.get("add_offset", 0))
                else:
                    dataset = data.copy()

                fields[var_name] = dataset

                LOG.debug("long_name: %s", str(var.attrs['long_name']))
                LOG.debug("Var = %s, shape = %s",
                          str(var_name), str(dataset.shape))

                processed |= set([var_name])

            non_processed = set(variables.keys()) - processed
            if len(non_processed) > 0:
                LOG.warning("Remaining non-processed variables: %s",
                            str(non_processed))

            h5f.close()
            if unzipped:
                os.remove(unzipped)

            y0_ = swath_index
            y1_ = swath_index + self.granule_lengths[idx]
            swath_index = swath_index + self.granule_lengths[idx]

            for key in self.raw_data.keys():
                if key not in self.projectables:
                    continue
                try:
                    self.raw_data[key][y0_:y1_, :] = fields[key].data
                    self.mask[key][y0_:y1_, :] = fields[key].mask
                except ValueError:
                    LOG.exception('Mismatch in dimensions: y0_, y1_, ' +
                                  'fields[key].data.shape: %s %s %s',
                                  str(y0_), str(y1_), str(fields[key].data.shape))
                    raise

        return


class PPSReader(Reader):

    """Reader class for PPS files"""
    pformat = "nc_pps_l2"

    def __init__(self, *args, **kwargs):
        Reader.__init__(self, *args, **kwargs)

    def load(self, satscene, **kwargs):
        """Read data from file and load it into *satscene*.
        """

        prodfilenames = kwargs.get('filename')
        time_interval = kwargs.get('time_interval')

        PPS_PRODUCTS = ['CMA', 'CT', 'CTTH', 'PC', 'CPP']
        products = satscene.channels_to_load & set(PPS_PRODUCTS)
        if len(products) == 0:
            LOG.debug("No PPS cloud products to load, abort")
            return

        try:
            area_name = satscene.area_id or satscene.area.area_id
        except AttributeError:
            area_name = "satproj_?????_?????"

        conf = ConfigParser()
        configfile = os.path.join(CONFIG_PATH, satscene.fullname + ".cfg")
        LOG.debug("Read configfile %s", configfile)
        conf.read(configfile)

        try:
            cloud_product_geodir = conf.get(satscene.instrument_name + "-level3",
                                            "cloud_product_geodir",
                                            raw=True,
                                            vars=os.environ)
        except NoOptionError:
            cloud_product_geodir = None

        LOG.debug("cloud_product_geodir = %s", cloud_product_geodir)

        try:
            geolocation_product_name = conf.get(satscene.instrument_name + "-level3",
                                                "geolocation_product_name",
                                                raw=True,
                                                vars=os.environ)
        except NoOptionError:
            geolocation_product_name = None

        LOG.info("Products to load: %s", str(products))
        # Make the list of files for the products requested:

        if isinstance(time_interval, (tuple, set, list)) and len(time_interval) == 2:
            time_start, time_end = time_interval
        else:
            time_start, time_end = satscene.time_slot, None

        # File types expected:
        # S_NWC_CTTH_noaa19_33897_20150906T1240015Z_20150906T1240598Z.nc
        # or
        # W_XX-EUMETSAT-Darmstadt,SING+LEV+SAT,METOPB+CTTH_C_EUMS_20150916121900_15540.nc.bz2

        p_local = Parser(
            'S_NWC_{product:s}_{satid:s}_{orbit:5d}_{starttime:%Y%m%dT%H%M%S}{dummy:1d}Z_{starttime:%Y%m%dT%H%M%S}{dummy2:1d}Z')
        p_ears = Parser(
            'W_XX-EUMETSAT-Darmstadt,SING+LEV+SAT,{satid:s}+{product:s}_C_EUMS_{starttime:%Y%m%d%H%M%S}_{orbit:5d}')

        # If a list of files are provided to the load call, we disregard the
        # direcorty and filename specifications/definitions in the config file.

        if not prodfilenames:
            prodfilenames = get_filenames(
                satscene, products, conf, time_start, time_end, area_name)

        LOG.debug("Product files: %s", str(prodfilenames))
        prodfiles4product = {}
        prodfile_list = []
        geofile_list = []
        if prodfilenames:
            if not isinstance(prodfilenames, (list, set, tuple)):
                prodfilenames = [prodfilenames]
            for fname in prodfilenames:
                # Only standard NWCSAF/PPS and EARS-NWC naming accepted!
                if (os.path.basename(fname).startswith("S_NWC") or
                        os.path.basename(fname).startswith("W_XX-EUMETSAT")):
                    if os.path.basename(fname).startswith("S_NWC"):
                        p__ = p_local
                    else:
                        p__ = p_ears
                    prodfile_list.append(fname)
                else:
                    LOG.info("Unrecognized NWCSAF/PPS file: %s", fname)
                    continue

                data = p__.parse(
                    os.path.basename(fname).split('.nc')[0])
                prodname = data['product']
                if geolocation_product_name and prodname == geolocation_product_name:
                    geofile_list.append(fname)

                if prodname not in prodfiles4product:
                    prodfiles4product[prodname] = []

                prodfiles4product[prodname].append(fname)

            geofiles4product = {}
            if not geolocation_product_name:
                # Assume geo-location is available in product files!
                LOG.info(
                    "Geo-location is supposed to be available in product files")
            else:
                # Go through all product files, and check if there is a
                # geo-location file matching the product file:
                geoname_list = [os.path.basename(f) for f in geofile_list]
                geodir_list = [os.path.dirname(f) for f in geofile_list]
                for fname in prodfile_list:
                    data = p__.parse(
                        os.path.basename(fname).split('.nc')[0] + '.nc')

                    prodname = data['product']
                    if prodname not in geofiles4product:
                        geofiles4product[prodname] = []

                    gname = os.path.basename(fname).replace(data['product'],
                                                            geolocation_product_name)
                    if gname in geoname_list:
                        # File was specifically provided
                        LOG.debug(
                            "Geolocation identified in file list: %s", gname)
                        idx = geoname_list.index(gname)
                        directory = geodir_list[idx]
                    else:
                        LOG.debug(
                            "Geo-location file not specified for product. Try find one...")
                        if cloud_product_geodir:
                            directory = cloud_product_geodir
                            LOG.debug("DIR from config = %s", directory)
                        else:
                            directory = os.path.dirname(fname)
                            LOG.debug(
                                "DIR from product destination = %s", directory)

                    geofile = os.path.join(directory, gname)
                    LOG.debug("Geo filename: %s", str(geofile))
                    if not os.path.exists(geofile):
                        LOG.warning(
                            "Geo file for product does not exist: %s", geofile)

                    else:
                        geofiles4product[prodname].append(geofile)
                        LOG.debug("Geo files for product %s = %s",
                                  prodname, str(geofiles4product[prodname]))

            # Check that each product file has a corresponding geolocation
            # file:
            if geolocation_product_name:
                for prod in products:
                    if prod not in geofiles4product:
                        LOG.error("No product name %s in dict geofiles4product!",
                                  prod)
                        continue
                    if prod not in prodfiles4product:
                        LOG.error("No product name %s in dict prodfiles4product!",
                                  prod)
                        continue
                    if len(geofiles4product[prod]) != len(prodfiles4product[prod]):
                        LOG.error("Mismatch in number of product files and " +
                                  "matching geolocation files!")

        # geofiles4product is a dict listing all geo-locations files applicable
        # for each product.
        # prodfiles4product is a dict listing all product files for a given
        # product name

        # Reading the products
        classes = {"CTTH": CloudTopTemperatureHeight,
                   "CT": CloudType,
                   "CMA": CloudMask,
                   "PC": PrecipitationClouds,
                   "CPP": CloudPhysicalProperties
                   }
        nodata_mask = False

        read_external_geo = {}
        for product in products:
            LOG.debug("Loading %s", product)

            if product not in prodfiles4product:
                LOG.warning("No files found for product: %s", product)
                continue

            pps_band = PPSProductData(prodfiles4product[product]).read()
            chn = classes[product]()
            chn.read(pps_band)

            if not chn.name in satscene:
                LOG.info("Adding new channel %s", chn.name)
                satscene.channels.append(chn)

            # Check if geolocation is loaded:
            if not chn.area:
                read_external_geo[product] = satscene.channels[-1].name

        # Check if some 'channel'/product needs geolocation. If some
        # product does not have geolocation, get it from the
        # geofilename:
        from pyresample import geometry

        # Load geolocation
        for chn_name in read_external_geo.values():
            LOG.debug("ch_name = %s", str(chn_name))
            chn = satscene[chn_name]
            geofilenames = geofiles4product[chn_name]
            LOG.debug("Geo-files = %s", str(geofilenames))
            geoloc = PpsGeolocationData(chn.shape,
                                        chn.granule_lengths,
                                        geofilenames).read()

            try:
                satscene[chn.name].area = geometry.SwathDefinition(
                    lons=np.ma.masked_where(nodata_mask,
                                            geoloc.longitudes.data,
                                            copy=False),
                    lats=np.ma.masked_where(nodata_mask,
                                            geoloc.latitudes.data,
                                            copy=False))

                area_name = ("swath_" + satscene.fullname + "_" +
                             str(satscene.time_slot) + "_"
                             + str(chn.shape) + "_" +
                             chn.name)
                satscene[chn.name].area.area_id = area_name
                satscene[chn.name].area_id = area_name
            except ValueError:
                LOG.exception('Failed making a SwathDefinition: ' +
                              'min,max lons,lats = (%f %f") (%f,%f)',
                              geoloc.longitudes.data.min(),
                              geoloc.longitudes.data.max(),
                              geoloc.latitudes.data.min(),
                              geoloc.latitudes.data.max())
                LOG.warning("No geolocation loaded for %s", str(chn_name))

        # PpsGeolocationData.clear_cache()

        return


class CloudType(NwcSafPpsChannel):

    """CloudType PPS channel object"""

    def __init__(self):
        NwcSafPpsChannel.__init__(self)
        self.name = "CT"


class CloudTopTemperatureHeight(NwcSafPpsChannel):

    """Cloud top temperature and height PPS channel object"""

    def __init__(self):
        NwcSafPpsChannel.__init__(self)
        self.name = "CTTH"


class CloudMask(NwcSafPpsChannel):

    """Cloud mask PPS channel object"""

    def __init__(self):
        NwcSafPpsChannel.__init__(self)
        self.name = "CMA"


class PrecipitationClouds(NwcSafPpsChannel):

    """Precipitation clouds PPS channel object"""

    def __init__(self):
        NwcSafPpsChannel.__init__(self)
        self.name = "PC"


class CloudPhysicalProperties(NwcSafPpsChannel):

    """Cloud physical proeperties PPS channel"""

    def __init__(self):
        NwcSafPpsChannel.__init__(self)
        self.name = "CPP"


def get_lonlat_into(filename, out_lons, out_lats, out_mask):
    """Read lon,lat from hdf5 file"""
    LOG.debug("Geo File = " + filename)

    shape = out_lons.shape

    unzipped = unzip_file(filename)
    if unzipped:
        filename = unzipped

    md = HDF5MetaData(filename).read()

    reduced_grid = False
    h5f = h5py.File(filename, 'r')

    if "column_indices" in h5f.keys():
        col_indices = h5f["column_indices"][:]
    if "row_indices" in h5f.keys():
        row_indices = h5f["row_indices"][:]
    if "nx_reduced" in h5f:
        col_indices = h5f["nx_reduced"][:]
    if "ny_reduced" in h5f:
        row_indices = h5f["ny_reduced"][:]

    for key in md.get_data_keys():
        if ((key.endswith("lat") or key.endswith("lon")) or
                (key.endswith("lat_reduced") or key.endswith("lon_reduced"))):
            lonlat = h5f[key]
            fillvalue = lonlat.attrs["_FillValue"]
        else:
            continue

        if key.endswith("lat"):
            lonlat.read_direct(out_lats)
        elif key.endswith("lon"):
            lonlat.read_direct(out_lons)
        elif key.endswith("lat_reduced"):
            lat_reduced = lonlat[:]
            reduced_grid = True
        elif key.endswith("lon_reduced"):
            lon_reduced = lonlat[:]

    if reduced_grid:
        from geotiepoints import SatelliteInterpolator

        cols_full = np.arange(shape[1])
        rows_full = np.arange(shape[0])

        satint = SatelliteInterpolator((lon_reduced, lat_reduced),
                                       (row_indices,
                                        col_indices),
                                       (rows_full, cols_full))
        out_lons[:], out_lats[:] = satint.interpolate()

    out_mask[:] = np.logical_and(out_lats <= fillvalue, out_lons <= fillvalue)

    h5f.close()
    if unzipped:
        os.remove(unzipped)


def globify(filename):
    filename = filename.replace("%Y", "????")
    filename = filename.replace("%m", "??")
    filename = filename.replace("%d", "??")
    filename = filename.replace("%H", "??")
    filename = filename.replace("%M", "??")
    filename = filename.replace("%S", "??")
    return filename
