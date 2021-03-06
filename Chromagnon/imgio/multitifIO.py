from __future__ import division
import sys
try:
    from . import generalIO
except ImportError:
    import generalIO
import six
import tifffile
from tifffile import tifffile as tifff

    
import numpy as N

WRITABLE_FORMATS = ('tif', 'tiff')
READABLE_FORMATS = WRITABLE_FORMATS + ('ome.tif', 'ome.tiff', 'lsm')


def _convertUnit(val, fromwhat='mm', towhat=u'\xb5'+'m'):
    factors = {'m': 0, 'mm': -3, u'\xb5'+'m': -6, 'nm': -9, 'micron': -6}
    factor = factors[fromwhat] - factors[towhat] or 0
    return float(val) * 10 ** factor


class MultiTiffReader(generalIO.GeneralReader):
    def __init__(self, fn):
        """
        fn: file name
        """
        generalIO.GeneralReader.__init__(self, fn)

    def openFile(self):
        """
        open a file for reading
        """
        self.fp = tifffile.TiffFile(self.fn)
        self.handle = self.fp.filehandle
        
        self.readHeader()

    def readHeader(self):
        self.readMetaData()

        s = self.fp.series[0]
        shape = s.shape

        if 0:#self.fp.is_micromanager:
            nw = self.metadata['Channels']
            nz = self.metadata['Slices']
            nt = self.metadata['Frames']
            axes = s.axes.replace('C', 'W')
            imgSeq = self.findImgSequence(axes[:-2])
        elif self.fp.is_imagej and not self.fp.is_micromanager and 'channels' in self.metadata:
            imgSeq = 1
            nw = self.metadata['channels']
            nz = self.metadata['slices']
            nt = self.metadata['images'] // (nw * nz)
                
            #waves = self.wave
            axes = 'TZCYXS'#'TZW'
        else:
            axes = s.axes.replace('S', 'W')    # sample (rgb)
            axes = axes.replace('C', 'W')      # color, emission wavelength
            axes = axes.replace('E', 'W')      # excitation wavelength
            if 'Z' not in axes:
                axes = axes.replace('I', 'Z')  # general sequence, plane, page, IFD
            elif 'W' not in axes:
                axes = axes.replace('I', 'W')
            elif 'T' not in axes:
                axes = axes.replace('I', 'T')
            if 'Z' not in axes:
                axes = axes.replace('Q', 'Z') # other
            elif 'W' not in axes:
                axes = axes.replace('Q', 'W')
            elif 'T' not in axes:
                axes = axes.replace('Q', 'T')
            imgSeq = self.findImgSequence(axes[:-2])


            nz = nt = nw = 1
            if 'Z' in axes:
                zaxis = axes.index('Z')
                nz = shape[zaxis]

            if 'T' in axes:
                taxis = axes.index('T')
                nt = shape[taxis]

            if 'W' in axes:
                waxis = axes.index('W')
                nw = shape[waxis]
        waves = generalIO.makeWaves(nw)

        
        p = self.fp.pages[0]

        # byte_count, offsets = _byte_counts_offsets
        self.dataOffset = p.offsets_bytecounts[0][0]
        if len(self.fp.pages) > 1:
            page1_offset     = self.fp.pages[1].offsets_bytecounts[0][0]
            page0_offset     = self.fp.pages[0].offsets_bytecounts[0][0]
            page0_byte_count = self.fp.pages[0].offsets_bytecounts[1][0]
            
            self._secExtraByteSize = page1_offset - page0_offset - page0_byte_count

        else:
            self._secExtraByteSize = 0


        waves = self.readChannelInfo(nw, waves)

        self.setDim(p.imagewidth, p.imagelength, nz, nt, nw, s.dtype, waves, imgSeq)
        #self.setPixelSize(p.samplesperpixel)
        
        self.axes = p.axes # axis of one section
        self.compress = p.compression

        # since imageJ stores all image metadata in the first page
        #if self.fp.is_imagej or self.readSec(0).ndim > 2:
        #    self.arr = self.fp.pages[0].asarray()

    def readMetaData(self):
        """
        read pixel size and extra info
        """
        self.ex_metadata = {}
        p = self.fp.pages[0]

        if self.fp.is_micromanager: # also is_ome and is_imagej
            self.metadata = meta = self.fp.micromanager_metadata['Summary']
            px = meta.get('PixelSize_um', 0.1)
            if not px:
                px = 0.1
            asp = meta.get('PixelAspect', 1)
            py = px * asp # is this correct?
            pz = meta.get('z-step_um', 0.3)
            if not pz:
                pz = 0.3
            self.setPixelSize(pz, py, px)
        
        elif self.fp.is_ome:
            if 'OME' in self.fp.ome_metadata:
                self.metadata = meta = self.fp.ome_metadata['OME']
            else:
                self.metadata = meta = self.fp.ome_metadata
            # pixel size
            px = meta['Image']['Pixels']
            pxlsiz = [0.1, 0.1, 0.1]
            for d, dim in enumerate(('Z', 'Y', 'X')):
                pxlsiz[d] = _convertUnit(px['PhysicalSize%s' % dim], px['PhysicalSize%sUnit' % dim])
            self.setPixelSize(*pxlsiz)

            if meta.get('StructuredAnnotations'):
                sas = meta['StructuredAnnotations']['XMLAnnotation']
                for sa in sas: # list
                    kv = sa['Value']['OriginalMetadata']
                    self.metadata[kv['Key']] = kv['Value']
                
        elif self.fp.is_imagej:
            self.metadata = meta = self.fp.imagej_metadata
            if 'spacing' in meta:
                pz = meta['spacing']
                xr = p.tags['XResolution']
                px = xr.value[1]/xr.value[0]
                yr = p.tags['YResolution']
                py = yr.value[1]/yr.value[0]
                unit = meta['unit']
                pz = _convertUnit(pz, unit)
                self.setPixelSize(pz, py, px)
            else:
                self.setPixelSize(0.3, 0.1, 0.1)

            if 'Info' in meta:
                for m in meta['Info'].split('\n')[13:]:
                    mm = m.split(' = ')
                    if len(mm) == 2:
                        key, val = mm
                        self.ex_metadata[key] = val

        elif self.fp.is_lsm:
            self.metadata = meta = self.fp.lsm_metadata
            pz = _convertUnit(meta['VoxelSizeZ'], 'm', 'micron')
            py = _convertUnit(meta['VoxelSizeY'], 'm', 'micron')
            px = _convertUnit(meta['VoxelSizeX'], 'm', 'micron')
            self.setPixelSize(pz, py, px)

        else:
            
            self.setPixelSize(0.3, 0.1, 0.1)


    def readChannelInfo(self, nw, waves):
        if self.fp.is_micromanager:
            # my code uses wavelength a lot, and therefore, string name is not accepted...
            #waves = self.metadata['ChNames']
            self.wave = N.arange(400, 700, 300//nw)[:nw]
        
        elif self.fp.is_ome:
            px = self.metadata['Image']['Pixels']
            for w in range(nw):
                channel = px['Channel'][w]
                unit = channel.get('EmissionWavelengthUnit', 'nm')
                wave = channel.get('EmissionWavelength')
                if wave is not None:
                    waves.append(_convertUnit(wave, unit, 'nm'))
        elif self.fp.is_imagej and 'Info' in self.metadata:
            for m in self.metadata['Info'].split('\n')[13:]:
                    mm = m.split(' = ')
                    if len(mm) == 2:
                        key, val = mm
                        # reading wavelength of tif from DV format ...
                        if key.startswith('Wavelength') and key.endswith('nm)'):
                            channel = int(key.split(' ')[1]) - 1
                            if channel < nw:
                                waves[channel] = eval(val)
        elif self.fp.is_lsm:
            channel = self.metadata['ChannelColors']['Colors']
            color_code = N.array((650, 515, 450, 0), N.float32)
            waves = []
            for w in range(nw):
                color = N.array(channel[w])
                waves.append(int(N.sum(color_code * color / N.sum(color))))
        
        elif 'waves' in self.metadata:
            waves = [eval(w) for w in self.metadata['waves'].split(',')]
        return waves

                                
    def seekSec(self, i=0):
        p = self.fp.pages[i]
        #byte_counts, offsets = p._byte_counts_offsets
        offsets, byte_counts = p.offsets_bytecounts#_byte_counts_offsets

        self.handle.seek(offsets[0])
        
    def readSec(self, i=None):
        """
        return the section at the number i in the file
        if i is None: return current position (not fast though)
        """
        if i is None:
            i = self.tellSec() + 1 # handle go back to the beggining of the page after reading...

        #if 0:#self.fp.is_imagej or hasattr(self, 'arr'):
        #    return self.arr[int(i)]
        #else:
        
        arr = self.fp.pages[int(i)].asarray()
        if arr.ndim == 3: # h.axes == 'SYX'
            arr = arr[self.axes_w]
        return arr



class MultiTiffWriter(generalIO.GeneralWriter):
    def __init__(self, fn, mode=None, style='imagej', software='multitifIO.py', extra_metadata={}):
        """
        mode is 'wb' whatever the value is...
        style: 'imagej', 'ome'...
        """
        self.style = style #imagej = imagej
        self.metadata = {}
        self.software = software
        self.ex_metadata = extra_metadata
        self.extratags = ()
        self.init = False

        generalIO.GeneralWriter.__init__(self, fn, mode)


    def openFile(self):
        """
        open a file for reading
        """
        imagej = self.style == 'imagej'
        self.fp = tifffile.TiffWriter(self.fn, software=self.software, imagej=imagej)#bigtiff=not(imagej), imagej=imagej)

        self.handle = self.fp._fh
        self.dataOffset = self.handle.tell()
        
    def doOnSetDim(self):
        # ImageJ's dimension order is always TZCYXS
        if self.style == 'imagej': 
            self.imgSequence = 1
            self.metadata.update(self._makeImageJMetadata())
        elif self.style == 'ome':
            self.metadata.update(self.makeOMEMetadata())
        else:
            self.metadata.update(self.ex_metadata)

        unit = 10**6
        self.res = [(int(p), unit) for p in 1/self.pxlsiz[-2:] * unit]
            
        
    def writeSec(self, arr, i=None):
        if not self.init:
            self.doOnSetDim()
            test = N.zeros((self.ny, self.nx), self.dtype)
            self.dataOffset, self._secByteSize = self.fp.save(test, resolution=self.res, metadata=self.metadata, returnoffset=True)
            self.handle.seek(self.handle.tell() - self._secByteSize)

            self.init = True
        
        if i is not None:
            self.seekSec(i)

        offset, sec = self.fp.save(arr, resolution=self.res, metadata=self.metadata, returnoffset=True, extratags=self.extratags)
        #if (offset - self._secByteSize * int(i)) != self.dataOffset or sec != self._secByteSize:
        #    raise ValueError('offset %i -> %i, sec %i -> %i' % (self.dataOffset, offset, self._secByteSize, sec))

    def _makeImageJMetadata(self):
        """
        "Info" field does not work...
        Somebody tell me why
        """
        #ex_meta = ''
        #for key, val in self.ex_metadata.items():
        #if 
        
        metadata = {'ImageJ': '1.51h',
                    'images': self.nsec,
                    'channels': self.nw,
                    'slices': self.nz,
                    'hyperstack': self.ndim > 3,
                    'mode': 'grayscale',
                    'unit': 'micron',
                    'spacing': self.pxlsiz[0],
                    'loop': False,
                    # my field goes to "description"
                    'waves': ','.join([str(wave) for wave in self.wave[:self.nw]])
                    }
                    #'min': 0.0,
                    #'max': 0.0,
                    #'Info': infostr}
        metadata.update(self.ex_metadata)
        if hasattr(self, 'prev_metadata'):
            #print('writing ij')
            #metadata['IJMetadata'] = {'info': self.prev_metadata['Info']}
            #print(metadata.keys())

            ### middle of writing
            middle_of_writing="""
            nmeta = 1
            ntypes = 8 * nmeta + 4 # 4 is "IJIJ" and 8 is the byte count
            self.extratags = [('IJMetadata', 's', 0, 'IJIJinfo1'+self.prev_metadata['Info'], True), # (code, dtype, count, value, writeonce)
                         ('IJMetadataByteCounts', 'I', 2, (ntypes, len(self.prev_metadata['Info']) * 2), True)]"""
            
        return metadata

    notworking='''
    def makeOMEMetadata(self):
        UUID = 'urn:uuid:58600c40-5b05-494a-8a8c-a85ed62c6f99'
        def makeTiffData():
            data = [0] * self.nsec
            for t in range(self.nt):
                for w in range(self.nw):
                    for z in range(self.nz):
                        i = self.findFileIdx(t=t, w=w, z=z)
                        data[i] = {'UUID': {'FilenName': self.fn,
                                                'value': UUID},
                                    'IFD': i,
                                    'PlaneCount': 1,
                                    'FirstT': t,
                                    'FirstC': w,
                                    'FirstZ': z}
            return data
                
        
        d = {'OME':{
                '{http://www.w3.org/2001/XMLSchema-instance}schemaLocation': 'http://www.openmicroscopy.org/Schemas/OME/2016-06 http://www.openmicroscopy.org/Schemas/OME/2016-06/ome.xsd',
                'UUID': UUID,
                'Creator': self.software,
                'StructuredAnnotations': None,
                'Image':{
                     'ID': 'Image:0',
                     'Name': 'default.png',
                     'Pixels':
                         {'ID': 'Pixels:0',
                          'Type': pixeltype_to_ome(self.dtype),
                          'BigEndian': sys.byteorder == 'big',
                          'Interleaved': False,
                          'DimensionOrder': imgSeq_to_ome(self.imgSequence),
                          'SizeX': self.nx,
                          'SizeY': self.ny,
                          'SizeZ': self.nz,
                          'SizeC': self.nw,
                          'SizeT': self.nt,
                          'PhysicalSizeX': float(self.pxlsiz[-1]),
                          'PhysicalSizeY': float(self.pxlsiz[-2]),
                          'PhysicalSizeZ': float(self.pxlsiz[-3]),
                          'PhysicalSizeXUnit': u'\xb5'+'m',
                          'PhysicalSizeYUnit': u'\xb5'+'m',
                          'PhysicalSizeZUnit': u'\xb5'+'m',
                          'TiffData': makeTiffData()}
                 }}}
        return d

    #def makeOMEMetadata(self):
        
    
pxltypes_dtype = {N.int8: 'int8',
                    N.int16: 'int16',
                    N.int32: 'int32',
                    N.uint8: 'uint8',
                    N.uint16: 'uint16',
                    N.uint32: 'uint32',
                    N.float32: 'float',
                    N.bool_: 'bit',
                    N.float64: 'double',
                    N.complex64: 'complex',
                    N.complex128: 'double complex'
                    }
imgOrders = ['XYZTC',
             'XYCZT',
             'XYZCT',
             'XYTZC',
             'XYCTZ',
             'XYTCZ']

def pixeltype_to_ome(dtype):
    if hasattr(dtype, 'type'):
        return pxltypes_dtype[dtype.type]
    else:
        return pxltypes_dtype[dtype]

def ome_to_dtype(pixeltype):
    keys = list(pxltypes_dtype.keys())
    for key in keys:
        if pxltypes_dtype[key] == pixeltype:
            return key
    raise ValueError('pixeltype %s was not found' % pixeltype)

def imgSeq_to_ome(imgSequence):
    return imgOrders[imgSequence]'''
    
def xml2dict(xml, sanitize=True, prefix=None):
    """Return XML as dict.

    >>> xml2dict('<?xml version="1.0" ?><root attr="name"><key>1</key></root>')
    {'root': {'key': 1, 'attr': 'name'}}

    """
    from collections import defaultdict  # delayed import
    from xml.etree import cElementTree as etree  # delayed import

    at = tx = ''
    if prefix:
        at, tx = prefix

    def etree2dict(t):
        # adapted from https://stackoverflow.com/a/10077069/453463
        key = t.tag
        if sanitize:
            key = key.rsplit('}', 1)[-1]
        d = {key: {} if t.attrib else None}
        children = list(t)
        if children:
            dd = defaultdict(list)
            for dc in map(etree2dict, children):
                for k, v in dc.items():
                    dd[k].append(astype(v))
            d = {key: {k: astype(v[0]) if len(v) == 1 else astype(v)
                       for k, v in dd.items()}}
        if t.attrib:
            d[key].update((at + k, astype(v)) for k, v in t.attrib.items())
        if t.text:
            text = t.text.strip()
            if children or t.attrib:
                if text:
                    d[key][tx + 'value'] = astype(text)
            else:
                d[key] = astype(text)
        return d

    return etree2dict(etree.fromstring(xml))

tifff.xml2dict = xml2dict


def astype(value):
    if isinstance(value, six.string_types) and (value[0].isdigit() or value[-1].isdigit()):
        if value.isdigit():
            return int(value)
        else:
            try:
                return float(value)
            except:
                try:
                    return tifff.asbool(value)
                except:
                    return value
    else:
        try:
            return tifff.asbool(value)
        except:
            return value

def testit(fn, outfn='testImageJ.tif'):
    r = MultiTiffReader(fn)
                
    ww = MultiTiffWriter(outfn, imagej=True)
    ww.setFromReader(r)
    for t in range(r.nt):
        for w in range(r.nw):
            for z in range(r.nz):
                arr = r.getArr(t=t, w=w, z=z)
                ww.writeArr(arr, t=t, w=w, z=z)
    ww.close()
    rr = MultiTiffReader(outfn)
    return rr.asarray()
