#!/usr/bin/python
"""Dynamic pxelinux.cfg and provisioning through FUSE.

This application's intended purpose is to be mounted to the pxelinux.cfg
directory of your tftpd root used for PXE (network) booting. 

When the PXE client requests a configuration file for its IP address, the
FUSE filesystem will prepare a new root partition for the client by
creating mount points, mounting an AUFS merge of the read-only root partition
specified by root_dir and an empty read-write directory. Each new PXE client
is given a fresh and unique AUFS overlay. The newly created AUFS filesystem is
then added to the NFS exports with permissions given to the client's IP.

This has been tested using tftpd-ha along with kernel-nfs-server.

Be sure to edit pxebootfs.cfg to fit your configuration.

requirements:
 python-fuse
 aufs and fuse filesystem support
 
example:
 ./fuse_pxebootfs.py /var/lib/tftpd/pxelinux.cfg -o allow_other

"""
import fuse
import errno, os, time, stat, sys, re
import ConfigParser
fuse.fuse_python_api = (0, 2)

__author__ = "Mark Riedesel <mark@klowner.com>"
__version__ = "0.1"
__date__ = "?"
__license__ = "GNU General Public License (version 2)"
CONF_SECTION = 'PXEBOOTFS'

def hex2ip(ip_hex):
    """Converts 'C0A80101' to '192.168.1.1'"""
    return '.'.join(map(lambda x: str(int(x, 16)), (
        ip_hex[0:2], ip_hex[2:4], ip_hex[4:6], ip_hex[6:8]))
        )

def ip2hex(ip_addr):
    """Converts '192.168.1.1' to 'C0A80101', etc."""
    return "".join( map( lambda x: "%02X" % int(x), ip_addr.split('.')) )

class PXEBootError(Exception):
    pass

class Stat(fuse.Stat):
    def __init__(self, timestamp):
        fuse.Stat.__init__(self)
        self.st_atime = int(timestamp)
        self.st_ctime = int(timestamp)
        self.st_mtime = int(timestamp)
        self.st_dev = 0
        self.st_gid = os.getgid()
        self.st_ino = 0
        self.st_mode = 0
        self.st_size = 0
        self.st_uid = os.getuid()
        self.st_nlink = 0
        
class PXEBootFS(fuse.Fuse):
    def __init__(self, *args, **kwargs):
        fuse.Fuse.__init__(self, *args, **kwargs)
        self.node_re = re.compile(r'^([A-Z0-9]{8})$')

        # These are populated by load_template()
        self.conf_root_dir = None
        self.conf_node_dir = None
        self.conf_overlay_dir = None
        self.pxe_template = None
        self._next_fsid = 100000

        # Further init stuff
        self.load_config()
        self.load_template(self.conf_pxe_template)
        self.root_time = 0

    def fsinit(self):
        self.root_time = time.time()

    def load_config(self):
        """ Loads configuration files from various expected places.
        """

        config = ConfigParser.ConfigParser()
        config.read([
            'pxebootfs.cfg', 
            os.path.expanduser('~/.pxebootfs.cfg'),
            '/etc/pxebootfs/pxebootfs.cfg',
            ])
        self.conf_pxe_template = config.get(CONF_SECTION, 'pxe_template')
        self.conf_node_dir = config.get(CONF_SECTION, 'node_dir')
        self.conf_root_dir = config.get(CONF_SECTION, 'root_dir')
        self.conf_overlay_dir = config.get(CONF_SECTION, 'overlay_dir')
        self._next_fsid = int(config.get(CONF_SECTION, 'start_fsid'))

    def load_template(self, path):
        """
        Load the pxelinux.cfg template from the disk and
        calculate the filesize.
        """
        self.pxe_template = file(path,'r').read( os.stat(path).st_size )
        if self.pxe_template.find('<NODE>') == -1:
            raise PXEBootError('Could not find <NODE> in PXE config template')
        
        self.pxe_template_length = len(self.pxe_template) - len('<NODE>') + 8

    def verify_permissions(self):
        """
        Explore the node, root, and overlay directories to verify the
        required permissions are available. Raises a PXEBootError
        if there is a problem.
        """
        for dir in [ self.conf_node_dir, self.conf_root_dir,
                   self.conf_overlay_dir ]:
            if not os.path.isdir(dir):
                err = "Directory does not exist: %s" % dir
                raise PXEBootError(err)

        for dir in [ self.conf_node_dir, self.conf_overlay_dir ]:
            if not os.access(dir, os.R_OK | os.W_OK ):
                err = "Don't have read/write permissions on: %s" % dir
                raise PXEBootError(err)

    def get_node_list(self):
        """
        Returns a list of nodes in 8-char hex form. This method will
        only return nodes which are actively used as mountpoints and
        also removes unused mountpoints.

        It would probably be better to remove mountpoints based on
        if they're actively shared via NFS.
        """

        nodes = []
        for node in os.listdir( self.conf_node_dir ):
            ndir = os.path.join( self.conf_node_dir, node )
            if os.path.ismount(ndir):
                nodes.append( os.path.basename( ndir ))
            elif os.path.isdir(ndir):
                os.rmdir(ndir)
        return nodes

    def get_ip_list(self):
        """
        Return list of active IPs. This method simply calls get_node_list()
        and converts the node representation to dotted quads.
        """
        for node in self.get_node_list():
            yield hex2ip( os.path.basename(node) )

    def setup_node(self, node):
        """
        Called when a new node is to be provisioned.
        Creates all necessary directories, ensures the AUFS mount is in place,
        and shares the new mount via NFS.
        """

        node_path = os.path.join( self.conf_node_dir, node )
        if not os.path.exists( node_path ):
            os.mkdir( node_path )

        overlay_path = os.path.join( self.conf_overlay_dir, node )
        if not os.path.exists( overlay_path ):
            os.mkdir( overlay_path )

        root_path = self.conf_root_dir

        if not os.path.ismount( node_path ):
            os.system('mount -t aufs -o br:%(rw)s=rw:%(ro)s=ro none %(mnt)s' % {
                'ro': root_path, 'rw': overlay_path, 'mnt': node_path
                })
    
        os.system(
            'exportfs -o rw,no_root_squash,fsid=%(fsid)s %(ip)s:%(mnt)s' % {
                'mnt': node_path, 'fsid': str(self.get_next_fsid()), 
                'ip':hex2ip(node),
             })

        return node

    def get_pxe_data(self, node):
        """
        Return the contents of the PXE config template
        except replace all instances of '<NODE>' with the hex
        version of the client's IP
        """
        return self.pxe_template.replace('<NODE>', node)

    def getattr(self, path):    
        """FUSE getattr handler"""
        status = Stat(self.root_time)

        if path == '/' or path == '/by-ip':
            status.st_mode = stat.S_IFDIR | 0755
            status.st_size = 4096
            status.st_nlink = 2

        elif path.startswith('/by-ip/') and len(path) > len('/by-ip'):
            status.st_mode = stat.S_IFLNK | 0755
            status.st_size = 4096
            status.st_nlink = 2         

        elif self.node_re.match( os.path.basename(path) ):
            status.st_mode = stat.S_IFREG | 0666
            status.st_size = self.pxe_template_length
            status.st_nlink = 1

        else:
            return -errno.ENOENT

        return status

    def readdir(self, path, offset):
        "FUSE readdir handler"
        dir_ents = ['.', '..']

        if path == '/':
            dir_ents.extend(['by-ip'])
            dir_ents.extend( self.get_node_list() )

        elif path == '/by-ip':
            dir_ents.extend( self.get_ip_list() )
            
        for ent in dir_ents:
            yield fuse.Direntry(ent)

    def get_next_fsid(self):
        """
        Return the next filesystem ID.
        This could be improved to actually check and see
        if the filesystem ID is available, rather than just
        depending on returning a big, iterating number.
        """
        fsid = self._next_fsid
        self._next_fsid += 1
        return fsid

    def open( self, path, flags ):
        "FUSE open handler"
        node = os.path.basename(path)
        if self.node_re.match( node ):
            return 0
        return -1

    def read(self, path, length, offset):
        "FUSE read handler"
        node = os.path.basename(path)
        if self.node_re.match( node ):
            try:
                self.setup_node( node )
            except OSError, err:
                return str(err)
            return self.get_pxe_data(node)[offset:offset+length]
        return ''

    def readlink(self, path):
        "FUSE readlink handler"
        if path.startswith('/by-ip'):
            node = ip2hex( os.path.basename( path ) )
            return os.path.join( self.conf_node_dir, node )
        return '.'

if __name__ == '__main__':
    USAGE = ("fuse_pxebootfs version %(version)s (%(date)s)\n"
            "Usage: %(app)s <mount point> [fuse options].\n\n"
            "Example: %(app)s /lib/tftpd/pxelinux.cfg/\n"
            ) % { 'app': sys.argv[0], "version": __version__,
                  'date': __date__, 'license': __license__ }

    PXEFS = PXEBootFS(usage=USAGE)
    PXEFS.parse(values=PXEFS)
    try:
        PXEFS.verify_permissions()
        PXEFS.main()
    except PXEBootError, error:
        print "ERROR:", error

    sys.exit(0)

