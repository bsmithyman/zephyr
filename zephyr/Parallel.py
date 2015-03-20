from IPython.parallel import Client, parallel, Reference, require, depend, interactive
import numpy as np

DEFAULT_MPI = True
MPI_BELLWETHERS = ['PMI_SIZE', 'OMPI_UNIVERSE_SIZE']

def cdSame(rc):
    import os

    dview = rc[:]

    home = os.getenv('HOME')
    cwd = os.getcwd()

    @interactive
    def cdrel(relpath):
        import os
        home = os.getenv('HOME')
        fullpath = os.path.join(home, relpath)
        try:
            os.chdir(fullpath)
        except OSError:
            return False
        else:
            return True

    if cwd.find(home) == 0:
        relpath = cwd[len(home)+1:]
        return all(rc[:].apply_sync(cdrel, relpath))

def adjustMKLVectorization(nt=1):
    try:
        import mkl
    except ImportError:
        pass
    finally:
        mkl.set_num_threads(nt)

class RemoteInterface(object):

    def __init__(self, profile=None, MPI=None):

        if profile is not None:
            pupdate = {'profile': profile}
        else:
            pupdate = {}

        pclient = Client(**pupdate)

        if not cdSame(pclient):
            print('Could not change all workers to the same directory as the client!')

        dview = pclient[:]
        dview.block = True
        dview.clear()

        remoteSetup = '''
        import os'''

        parMPISetup = ''' 
        from mpi4py import MPI
        comm = MPI.COMM_WORLD
        rank = comm.Get_rank()''' 

        for command in remoteSetup.strip().split('\n'):
            dview.execute(command.strip())

        dview.scatter('rank', pclient.ids, flatten=True)

        dview.apply(adjustMKLVectorization)

        self.useMPI = False
        MPI = DEFAULT_MPI if MPI is None else MPI
        if MPI:
            MPISafe = False

            for var in MPI_BELLWETHERS:
                MPISafe = MPISafe or all(dview["os.getenv('%s')"%(var,)])

            if MPISafe:
                for command in parMPISetup.strip().split('\n'):
                    dview.execute(command.strip())
                ranks = dview['rank']
                reorder = [ranks.index(i) for i in xrange(len(ranks))]
                dview = pclient[reorder]
                dview.block = True
                dview.activate()

                # Set up necessary parts for broadcast-based communication
                self.e0 = pclient[reorder[0]]
                self.e0.block = True
                self.comm = Reference('comm')

            self.useMPI = MPISafe

        self.pclient = pclient
        self.dview = dview
        self.lview = pclient.load_balanced_view()

        # Generate 'par' object for Problem to grab
        self.par = {
            'pclient':      self.pclient,
            'dview':        self.dview,
            'lview':        self.pclient.load_balanced_view(),
        }

    def __setitem__(self, key, item):

        if self.useMPI:
            self.e0[key] = item
            code = 'if rank != 0: %(key)s = None\n%(key)s = comm.bcast(%(key)s, root=0)'
            self.dview.execute(code%{'key': key})

        else:
            self.dview[key] = item

    def __getitem__(self, key):

        if self.useMPI:
            code = 'temp_%(key)s = None\ntemp_%(key)s = comm.gather(%(key)s, root=%(root)d)'
            self.dview.execute(code%{'key': key, 'root': 0})
            item = self.e0['temp_%s'%(key,)]
            self.e0.execute('del temp_%s'%(key,))

        else:
            item = self.dview[key]

        return item

    def reduce(self, key, axis=None):

        if self.useMPI:
            code = 'temp_%(key)s = comm.reduce(%(key)s, root=%(root)d)'
            self.dview.execute(code%{'key': key, 'root': 0})

            # if axis is not None:
            #     code = 'temp_%(key)s = temp_%(key)s.sum(axis=%(axis)d)'
            #     self.e0.execute(code%{'key': key, 'axis': axis})

            item = self.e0['temp_%s'%(key,)]
            self.dview.execute('del temp_%s'%(key,))

        else:
            item = reduce(np.add, self.dview[key])

        return item

    def reduceMul(self, key1, key2, axis=None):

        if self.useMPI:
            # Gather
            code_reduce = 'temp_%(key)s = comm.reduce(%(key)s, root=%(root)d)'
            self.dview.execute(code_reduce%{'key': key1, 'root': 0})
            self.dview.execute(code_reduce%{'key': key2, 'root': 0})

            # Multiply
            code_mul = 'temp_%(key1)s%(key2)s = temp_%(key1)s * temp_%(key2)s'
            self.e0.execute(code_mul%{'key1': key1, 'key2': key2})

            # # Potentially sum
            # if axis is not None:
            #     code = 'temp_%(key1)s%(key2)s = temp_%(key1)s%(key2)s.sum(axis=%(axis)d)'
            #     self.e0.execute(code%{'key1': key1, 'key2': key2, 'axis': axis})

            # Pull
            item = self.e0['temp_%(key1)s%(key2)s'%{'key1': key1, 'key2': key2}]

            # Clear
            self.dview.execute('del temp_%s'%(key1,))
            self.dview.execute('del temp_%s'%(key2,))
            self.e0.execute('del temp_%(key1)s%(key2)s'%{'key1': key1, 'key2': key2})

        else:
            item1 = reduce(np.add, self.dview[key1])
            item2 = reduce(np.add, self.dview[key2])
            item = item1 * item2

        return item
