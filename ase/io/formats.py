import collections
import functools
import inspect
import os
import sys

"""
format 'abc' abc.py: read_abc, write_abc.  Add to
does_not_accept_a_file_descriptor and stores_multiple_images lists




"""
from ase.atoms import Atoms
from ase.utils import import_module
from ase.db.core import parallel, parallel_generator

all_formats = {
    'abinit': ('ABINIT input file', '1F'),
    'aims': ('FHI-aims geometry file', '1S'),
    'aims-output': ('FHI-aims output', '1F'),
    'bundletrajectory': ('ASE bundle trajectory', '1S'),
    'castep': ('CASTEP output file', '1F'),
    'castep-cell': ('CASTEP geom file', '1S'),
    'castep-geom': ('CASTEP trajectory file', '1F'),
    'cfg': ('AtomEye configuration', '1F'),
    'cif': ('CIF-file', '+F'),
    'cmdft': ('CMDFT-file', '1F'),
    'cube': ('CUBE file', '1F'),
    'dacapo': ('Dacapo netCDF output file', '1F'),
    'dacapo-text': ('Dacapo text output', '1F'),
    'db': ('ASE SQLite database file', '+S'),
    'dftb': ('DftbPlus input file', '1S'),
    'eon': ('EON reactant.con file', '1F'),
    'eps': ('Encapsulated Postscript', '1S'),
    'espresso-in': ('Quantum espresso in file', '1F'),
    'espresso-out': ('Quantum espresso out file', '1F'),
    'etsf': ('ETSF format', '1S'),
    'exciting': ('exciting input', '1F'),
    'extxyz': ('Extended XYZ file', '+F'),
    'findsym': ('FINDSYM-format', '+F'),
    'gaussian': ('Gaussian com (input) file', '1F'),
    'gaussian-out': ('Gaussian output file', '1F'),
    'gen': ('DFTBPlus GEN format', '1F'),
    'gpaw-out': ('GPAW text output', '+S'),
    'gpw': ('GPAW restart-file', '1F'),
    'gromacs': ('Gromacs coordinates', '1S'),
    'gromos': ('Gromos96 geometry file', '1F'),
    'html': ('X3DOM HTML', '1S'),
    'iwm': ('?', '1F'),
    'json': ('ASE JSON database file', '+F'),
    'lammps-dump': ('LAMMPS dump file', '1F'),
    'mol': ('?', '1F'),
    'nwchem': ('NWChem input file', '1F'),
    'pdb': ('Protein Data Bank', '+F'),
    'png': ('Portable Network Graphics', '1F'),
    'postgresql': ('ASE PostgreSQL database file', '+S'),
    'pov': ('Persistance of Vision', '1S'),
    'res': ('SHELX format', '1S'),
    'sdf': ('?', '1F'),
    'struct': ('WIEN2k structure file', '1S'),
    'struct-out': ('SIESTA STRUCT file', '1F'),
    'traj': ('ASE trajectory', '+S'),
    'trj': ('Old ASE pickle trajectory', '+S'),
    'turbomole': ('TURBOMOLE coord file', '1F'),
    'turbomole-gradient': ('TURBOMOLE gradient file', '+F'),
    'v-sim': ('V_Sim ascii file', '1F'),
    'vasp': ('VASP POSCAR/CONTCAR file', '1F'),
    'vasp-out': ('VASP OUTCAR file', '1F'),
    'vasp-xdatcar': ('VASP XDATCAR file', '1F'),
    'vasp-xml': ('VASP vasprun.xml file', '1F'),
    'vti': ('VTK XML Image Data', '1F'),
    'vtu': ('VTK XML Unstructured Grid', '1F'),
    'x3d': ('X3D', '1S'),
    'xsd': ('Materials Studio file', '1F'),
    'xsf': ('XCrySDen Structure File', '+F'),
    'xyz': ('XYZ-file', '+F')}

# Special cases:
format2modulename = {
    'aims-out': 'aims',
    'aims-output': 'aims',
    'castep-cell': 'castep',
    'castep-geom': 'castep',
    'dacapo-text': 'dacapo',
    'espresso-in': 'espresso',
    'espresso-out': 'espresso',
    'gaussian-out': 'gaussian',
    'html': 'x3d',
    'json': 'db',
    'lammps-dump': 'lammpsrun',
    'postgresql': 'db',
    'struct': 'wien2k',
    'struct-out': 'siesta',
    'traj': 'trajectory',
    'trj': 'pickletrajectory',
    'turbomole-gradient': 'turbomole',
    'vasp-out': 'vasp',
    'vasp-xdatcar': 'vasp',
    'vasp-xml': 'vasp',
    'vti': 'vtkxml',
    'vtu': 'vtkxml'}

extension2format = {
    'cell': 'castep-cell',
    'com': 'gaussian',
    'con': 'eon',
    'exi': 'exciting',
    'g96': 'gromos',
    'geom': 'castep-geom',
    'gro': 'gromacs',
    'log': 'gaussian-out',
    'nw': 'nwchem',
    'out': 'espresso-out',
    'shelx': 'res'}

IOFormat = collections.namedtuple('IOFormat', 'read, write, single, acceptsfd')
ioformats = {}  # will be filled at run-time
        

def initialize(format):
    if format in ioformats:
        return
    _format = format.replace('-', '_')
    module_name = format2modulename.get(format, _format)
    try:
        module = import_module('ase.io.' + module_name)
    except ImportError:
        raise ValueError('File format not recognized: ' + format)
    read = getattr(module, 'read_' + _format, None)
    write = getattr(module, 'write_' + _format, None)
    if read and not inspect.isgeneratorfunction(read):
        read = functools.partial(wrap_old_read_function, read)
    if not read and not write:
        raise ValueError('File format not recognized: ' + format)
    code = all_formats[format][1]
    single = code[0] == '1'
    acceptsfd = code[1] == 'F'
    ioformats[format] = IOFormat(read, write, single, acceptsfd)
    

def get_ioformat(format):
    initialize(format)
    return ioformats[format]
    

def wrap_old_read_function(read, filename, index=None, **kwargs):
    if index is None:
        yield read(filename, **kwargs)
    else:
        for atoms in read(filename, index, **kwargs):
            yield atoms
        
        
@parallel
def write(filename, images, format=None, **kwargs):
    """Write Atoms object(s) to file.

    filename: str
        Name of the file to write to.
    images: Atoms object or list of Atoms objects
        A single Atoms object or a list of Atoms objects.
    format: str
        Used to specify the file-format.  If not given, the
        file-format will be taken from suffix of the filename.

    The accepted output formats:

    Many formats allow on open file-like object to be passed instead
    of ``filename``. In this case the format cannot be auto-decected,
    so the ``format`` argument should be explicitly given.

    The use of additional keywords is format specific."""

    if isinstance(filename, str):
        fd = None
        if filename == '-':
            fd = sys.stdout
            filename = None
        elif format is None:
            format = filetype(filename, read=False)
    else:
        fd = filename
        filename = None
        
    format = format or 'json'

    io = get_ioformat(format)

    if isinstance(images, Atoms):
        images = [images]
        
    if io.single:
        if len(images) > 1:
            raise ValueError('{0}-format can only store 1 Atoms object.'
                             .format(format))
        images = images[0]
        
    if io.write is None:
        raise ValueError("Can't write to {0}-format".format(format))
        
    # Special case for json-format:
    if format == 'json' and len(images) > 1:
        if filename is not None:
            io.write(filename, images, **kwargs)
            return
        raise ValueError("Can't write more than one image to file-descriptor"
                         'using json-format.')
        
    if io.acceptsfd:
        if fd is None:
            fd = open(filename, 'w')
        io.write(fd, images, **kwargs)
        fd.close()
    else:
        if fd is not None:
            raise ValueError("Can't write {0}-format to file-descriptor"
                             .format(format))
        io.write(filename, images, **kwargs)
    
    
def read(filename, index=None, format=None, **kwargs):
    """Read Atoms object(s) from file.

    filename: str
        Name of the file to read from.
    index: int or slice
        If the file contains several configurations, the last configuration
        will be returned by default.  Use index=n to get configuration
        number n (counting from zero).
    format: str
        Used to specify the file-format.  If not given, the
        file-format will be guessed by the *filetype* function.
        
    Many formats allow on open file-like object to be passed instead
    of ``filename``. In this case the format cannot be auto-decected,
    so the ``format`` argument should be explicitly given."""

    if isinstance(index, str):
        index = string2index(index)
    filename, index = parse_filename(filename, index)
    if index is None:
        index = -1
    if isinstance(index, (slice, str)):
        return list(_iread(filename, index, format, **kwargs))
    else:
        return next(_iread(filename, slice(index, None), format, **kwargs))
    
        
def iread(filename, index=None, format=None, **kwargs):
    if isinstance(index, str):
        index = string2index(index)
        
    filename, index = parse_filename(filename, index)
    
    if index is None:
        index = slice(None, None, None)
        
    if not isinstance(index, (slice, str)):
        index = slice(index, (index + 1) or None)
        
    for atoms in _iread(filename, index, format, **kwargs):
        yield atoms

            
@parallel_generator
def _iread(filename, index, format, **kwargs):
    if format is None:
        format = filetype(filename)

    io = get_ioformat(format)
    
    if not io.read:
        raise ValueError("Can't read from {0}-format".format(format))
        
    if io.single:
        start = index.start
        assert start is None or start == 0 or start == -1
        args = ()
    else:
        args = (index,)
        
    if isinstance(filename, str):
        if io.acceptsfd:
            fd = open(filename)
        else:
            fd = filename
    else:
        assert io.acceptsfd
        fd = filename
        
    # Make sure fd is closed in case loop doesn't finish:
    try:
        for atoms in io.read(fd, *args, **kwargs):
            yield atoms
    finally:
        if not isinstance(fd, str):
            fd.close()
    
    
def parse_filename(filename, index):
    if not isinstance(filename, str) or '@' not in filename:
        return filename, index
    newindex = None
    if ('.json@' in filename or
        '.db@' in filename or
        filename.startswith('pg://')):
        newfilename, newindex = filename.rsplit('@', 1)
    else:
        newfilename, newindex = filename.rsplit('@', 1)
        try:
            newindex = string2index(newindex)
        except ValueError:
            return filename, index
    if index is not None:
        raise ValueError('Only one index is allowed')
    return newfilename, newindex


def string2index(string):
    if ':' not in string:
        return int(string)
    i = []
    for s in string.split(':'):
        if s == '':
            i.append(None)
        else:
            i.append(int(s))
    i += (3 - len(i)) * [None]
    return slice(*i)


def filetype(filename, read=True):
    """Try to guess the type of the file."""
    if isinstance(filename, str):
        if os.path.isdir(filename):
            if os.path.basename(os.path.normpath(filename)) == 'states':
                return 'eon'
            return 'bundletrajectory'

        if filename.startswith('pg://'):
            return 'postgresql'
        
        basename = os.path.basename(filename)
        
        if '.' in basename:
            ext = filename.rsplit('.', 1)[-1].lower()
            if ext in ['xyz', 'cube', 'json']:
                return ext

        if 'POSCAR' in basename or 'CONTCAR' in basename:
            return 'vasp'
        if 'OUTCAR' in basename:
            return 'vasp-out'
        if 'XDATCAR' in basename:
            return 'vasp-xdatcar'
        if 'vasp' in basename and basename.endswith('.xml'):
            return 'vasp-xml'
        if basename == 'coord':
            return 'turbomole'
        if basename == 'gradient':
            return 'turbomole-gradient'
        if basename.endswith('I_info'):
            return 'cmdft'
        if basename == 'atoms.dat':
            return 'iwm'
            
        if not read:
            return extension2format.get(ext, ext)
    
        fd = open(filename, 'rb')
    else:
        ext = 'json'
        fd = filename
        if fd is sys.stdin:
            return 'json'
            
    data = fd.read(2000)
    if fd is not filename:
        fd.close()
        
    if len(data) == 0:
        raise IOError('Empty file: ' + filename)

    for format, magic in [('traj', b'AFFormatASE-Trajectory'),
                          ('trj', b'PickleTrajectory'),
                          ('etsf', b'CDF'),
                          ('turbomole', b'$coord'),
                          ('turbomole-gradient', b'$grad'),
                          ('dftb', b'Geometry')]:
        if data.startswith(magic):
            return format

    for format, magic in [('gpaw-out', b'  ___ ___ ___ _ _ _  \n'),
                          ('espresso-in', b'\n&system'),
                          ('espresso-in', b'\n&SYSTEM'),
                          ('aims-out', b'\nInvoking FHI-aims ...'),
                          ('lammps-dump', b'\nITEM: TIMESTEP\n'),
                          ('xsf', b'\nANIMSTEPS'),
                          ('xsf', b'\nCRYSTAL'),
                          ('xsf', b'\nSLAB'),
                          ('xsf', b'\nPOLYMER'),
                          ('xsf', b'\nMOLECULE'),
                          ('xsf', b'\nATOMS'),
                          ('dacapo-text',
                           b'&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&&\n')]:
        if magic in data:
            return format

    return extension2format.get(ext, ext)
        
    
if __name__ == '__main__':
    import optparse
    parser = optparse.OptionParser(
        usage='python -m ase.io.formats file ...',
        description='Determine file type(s).')
    opts, filenames = parser.parse_args()
    if filenames:
        n = max(len(filename) for filename in filenames) + 2
    for filename in filenames:
        try:
            format = filetype(filename)
            description, code = all_formats[format]
            if code[0] == '+':
                format += '+'
        except ValueError:
            format = '?'
            description = ''
            
        print('{0:{1}}{2} ({3})'.format(filename + ':', n,
                                        description, format))
