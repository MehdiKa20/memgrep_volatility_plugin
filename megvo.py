import sys
import os
import volatility.utils as utils
import volatility.obj as obj
import volatility.debug as debug
import volatility.win32.tasks as tasks
import volatility.win32.modules as modules
import volatility.plugins.taskmods as taskmods
#import volatility.plugins.vadinfo as vadinfo
import volatility.plugins.overlays.windows.windows as windows
import volatility.constants as constants
from volatility.renderers import TreeGrid
from volatility.renderers.basic import Address, Bytes
import os.path
import volatility.obj as obj
import volatility.plugins.taskmods as taskmods
import volatility.debug as debug #pylint: disable-msg=W0611
import volatility.constants as constants
from volatility.renderers import TreeGrid
from volatility.renderers.basic import Address
from volatility.plugins.vadinfo import VADInfo
# Vad Protections. Also known as page protections. _MMVAD_FLAGS.Protection,
# 3-bits, is an index into nt!MmProtectToValue (the following list). 
PROTECT_FLAGS = dict(enumerate([
    'PAGE_NOACCESS',
    'PAGE_READONLY',
    'PAGE_EXECUTE',
    'PAGE_EXECUTE_READ',
    'PAGE_READWRITE',
    'PAGE_WRITECOPY',
    'PAGE_EXECUTE_READWRITE',
    'PAGE_EXECUTE_WRITECOPY',
    'PAGE_NOACCESS',
    'PAGE_NOCACHE | PAGE_READONLY',
    'PAGE_NOCACHE | PAGE_EXECUTE',
    'PAGE_NOCACHE | PAGE_EXECUTE_READ',
    'PAGE_NOCACHE | PAGE_READWRITE',
    'PAGE_NOCACHE | PAGE_WRITECOPY',
    'PAGE_NOCACHE | PAGE_EXECUTE_READWRITE',
    'PAGE_NOCACHE | PAGE_EXECUTE_WRITECOPY',
    'PAGE_NOACCESS',
    'PAGE_GUARD | PAGE_READONLY',
    'PAGE_GUARD | PAGE_EXECUTE',
    'PAGE_GUARD | PAGE_EXECUTE_READ',
    'PAGE_GUARD | PAGE_READWRITE',
    'PAGE_GUARD | PAGE_WRITECOPY',
    'PAGE_GUARD | PAGE_EXECUTE_READWRITE',
    'PAGE_GUARD | PAGE_EXECUTE_WRITECOPY',
    'PAGE_NOACCESS',
    'PAGE_WRITECOMBINE | PAGE_READONLY',
    'PAGE_WRITECOMBINE | PAGE_EXECUTE',
    'PAGE_WRITECOMBINE | PAGE_EXECUTE_READ',
    'PAGE_WRITECOMBINE | PAGE_READWRITE',
    'PAGE_WRITECOMBINE | PAGE_WRITECOPY',
    'PAGE_WRITECOMBINE | PAGE_EXECUTE_READWRITE',
    'PAGE_WRITECOMBINE | PAGE_EXECUTE_WRITECOPY',
]))

# Vad Types. The _MMVAD_SHORT.u.VadFlags (_MMVAD_FLAGS) struct on XP has  
# individual flags, 1-bit each, for these types. The _MMVAD_FLAGS for all
# OS after XP has a member _MMVAD_FLAGS.VadType, 3-bits, which is an index
# into the following enumeration. 
MI_VAD_TYPE = dict(enumerate([
    'VadNone',
    'VadDevicePhysicalMemory',
    'VadImageMap',
    'VadAwe',
    'VadWriteWatch',
    'VadLargePages',
    'VadRotatePhysical',
    'VadLargePageSection',
]))

try:
    import yara
    has_yara = True
except ImportError:
    has_yara = False

try:
    import distorm3
    has_distorm3 = True
except ImportError:
    has_distorm3 = False

#--------------------------------------------------------------------------------
# functions 
#--------------------------------------------------------------------------------

def Disassemble(data, start, bits = '32bit', stoponret = False):
    """Dissassemble code with distorm3. 
    @param data: python byte str to decode
    @param start: address where `data` is found in memory
    @param bits: use 32bit or 64bit decoding 
    @param stoponret: stop disasm when function end is reached
    
    @returns: tuple of (offset, instruction, hex bytes)
    """

    if not has_distorm3:
        raise StopIteration

    if bits == '32bit':
        mode = distorm3.Decode32Bits
    else:
        mode = distorm3.Decode64Bits

    for o, _, i, h in distorm3.DecodeGenerator(start, data, mode):
        if stoponret and i.startswith("RET"):
            raise StopIteration
        yield o, i, h


class BaseYaraScanner(object):
    """An address space scanner for Yara signatures."""
    overlap = 1024

    def __init__(self, address_space = None, rules = None):
        self.rules = rules
        self.address_space = address_space

    def scan(self, offset, maxlen):
        # Start scanning from offset until maxlen:
        i = offset
        
        if isinstance(self.rules, list):
            rules = self.rules
        else:
            rules = [self.rules]

        while i < offset + maxlen:
            # Read some data and match it.
            to_read = min(constants.SCAN_BLOCKSIZE + self.overlap, offset + maxlen - i)
            data = self.address_space.zread(i, to_read)
            if data:
                for rule in rules:
                    for match in rule.match(data = data):
                        # We currently don't use name or value from the 
                        # yara results but they can be yielded in the 
                        # future if necessary. 
                        for moffset, _name, _value in match.strings:
                            if moffset < constants.SCAN_BLOCKSIZE:
                                yield match, moffset + i

            i += constants.SCAN_BLOCKSIZE

class VadYaraScanner(BaseYaraScanner):
    """A scanner over all memory regions of a process."""

    def __init__(self, task = None, **kwargs):
        """Scan the process address space through the Vads.
        Args:
          task: The _EPROCESS object for this task.
        """
        self.task = task
        BaseYaraScanner.__init__(self, address_space = task.get_process_address_space(), **kwargs)

    def scan(self, offset = 0, maxlen = None):
    
        if maxlen == None:
            vads = self.task.get_vads(skip_max_commit = True)
        else:
            filter = lambda x : x.Length < maxlen
            vads = self.task.get_vads(vad_filter = filter, 
                skip_max_commit = True)
        
        for vad, self.address_space in vads:
            for match in BaseYaraScanner.scan(self, vad.Start, vad.Length):
                yield match

class DiscontigYaraScanner(BaseYaraScanner):
    """A Scanner for Discontiguous scanning."""

    def scan(self, start_offset = 0, maxlen = None):
        contiguous_offset = 0
        total_length = 0
        for (offset, length) in self.address_space.get_available_addresses():
            # Skip ranges before the start_offset
            if self.address_space.address_compare(offset, start_offset) == -1:
                continue

            # Skip ranges that are too high (if maxlen is specified)
            if maxlen != None:
                if self.address_space.address_compare(offset, start_offset + maxlen) > 0:
                    continue

            # Try to join up adjacent pages as much as possible.
            if offset == contiguous_offset + total_length:
                total_length += length
            else:
                # Scan the last contiguous range.
                for match in BaseYaraScanner.scan(self, contiguous_offset, total_length):
                    yield match

                # Reset the contiguous range.
                contiguous_offset = offset
                total_length = length

        if total_length > 0:
            # Do the last range.
            for match in BaseYaraScanner.scan(self, contiguous_offset, total_length):
                yield match

#--------------------------------------------------------------------------------
# yarascan
#--------------------------------------------------------------------------------

class Megvo(taskmods.DllList):
    "Scan process or kernel memory with Yara signatures"

    def __init__(self, config, *args, **kwargs):
        taskmods.DllList.__init__(self, config, *args, **kwargs)
        config.add_option("ALL", short_option = 'A', default = False, action = 'store_true',
                        help = 'Scan both process and kernel memory')                
        config.add_option("CASE", short_option = 'C', default = False, action = 'store_true',
                        help = 'Make the search case insensitive')        
        config.add_option("KERNEL", short_option = 'K', default = False, action = 'store_true',
                        help = 'Scan kernel modules')
        config.add_option("WIDE", short_option = 'W', default = False, action = 'store_true',
                        help = 'Match wide (unicode) strings')
        config.add_option('YARA-RULES', short_option = 'Y', default = None,
                        help = 'Yara rules (as a string)')
        config.add_option('YARA-FILE', short_option = 'y', default = None,
                        help = 'Yara rules (rules file)')
        config.add_option('DUMP-DIR', short_option = 'D', default = None,
                        help = 'Directory in which to dump the files')
        config.add_option('SIZE', short_option = 's', default = 256,
                          help = 'Size of preview hexdump (in bytes)',
                          action = 'store', type = 'int')
        config.add_option('REVERSE', short_option = 'R', default = 0,
                          help = 'Reverse this number of bytes',
                          action = 'store', type = 'int')
        config.add_option('MAX-SIZE', short_option = 'M', default = 0x40000000, 
                          action = 'store', type = 'long', 
                          help = 'Set the maximum size (default is 1GB)') 
        #onfig.add_option("HEAPS-ONLY", short_option = 'H', default = False,action = 'store_true', help = 'Heaps only')

    def _compile_rules(self):
        """Compile the YARA rules from command-line parameters. 
        
        @returns: a YARA object on which you can call 'match'
        
        This function causes the plugin to exit if the YARA 
        rules have syntax errors or are not supplied correctly. 
        """
    
        rules = None
    	
        try:
            if self._config.YARA_RULES:
                s = self._config.YARA_RULES
                # Don't wrap hex or regex rules in quotes 
                if s[0] not in ("{", "/"): s = '"' + s + '"'
                # Option for case insensitive searches
                if self._config.CASE: s += " nocase"
                # Scan for unicode and ascii strings 
                if self._config.WIDE: s += " wide ascii"
                rules = yara.compile(sources = {
                            'n' : 'rule r1 {strings: $a = ' + s + ' condition: $a}'
                            })
            elif self._config.YARA_FILE and os.path.isfile(self._config.YARA_FILE):
                rules = yara.compile(self._config.YARA_FILE)
            else:
                debug.error("You must specify a string (-Y) or a rules file (-y)")
        except yara.SyntaxError, why:
            debug.error("Cannot compile rules: {0}".format(str(why)))
            
        return rules

    def _scan_process_memory(self, addr_space, rules):
		for task in self.filter_tasks(tasks.pslist(addr_space)):
			print("****************ruunning scan on process****************")
			print("***************************"+str(int(task.UniqueProcessId))+"*************************")
			print("********************************************************")
			task_space = task.get_process_address_space()
			scanner = VadYaraScanner(task = task, rules = rules)
			filter = lambda x : (x.Length < 1073741824)
			list_of_heaps=[]
			list_of_stacks=[]
			list_of_modules=[]
			#extraction of the heaps of the process
			heaps = task.Peb.ProcessHeaps.dereference()
			#extraction of the modules of the process 
			modules = [mod.DllBase for mod in task.get_load_modules()]
			#extraction of the stacks of the process
			stacks = []

			for thread in task.ThreadListHead.list_of_type("_ETHREAD", "ThreadListEntry"):
				teb = obj.Object("_TEB",offset = thread.Tcb.Teb,vm = task.get_process_address_space())
				if teb:
					
					stacks.append(teb.NtTib.StackBase)
			#searching of the matched string inside these data sections a.k.a heaps,stacks and modules
			for vad, _addrspace in task.get_vads(vad_filter = filter, skip_max_commit = True):
				if vad.Start in heaps:
					print("This memory is allocated for the heap of the process it ranges from "+hex(vad.Start)+"-"+ hex(vad.End))
					list_of_heaps.append((vad.Start))
					list_of_heaps.append((vad.End))
				if vad.Start in stacks:
					print("This memory is allocated for the stack of the process it ranges from "+hex(vad.Start)+"-"+ hex(vad.End))
					list_of_stacks.append((vad.Start))
					list_of_stacks.append((vad.End))
				if vad.Start in modules:
					print("This memory is allocated for the modules of the process it ranges from "+hex(vad.Start)+"-"+ hex(vad.End))
					list_of_modules.append((vad.Start))
					list_of_modules.append((vad.End))

			for hit, address in scanner.scan(maxlen = self._config.MAX_SIZE):
				i=0
				found=False
				while i<len(list_of_heaps):
					if (address - self._config.REVERSE) in range(list_of_heaps[i],list_of_heaps[i+1]):
						print("String is in the process's allocated Heap !!!")
						print('\n')
						found=True
						break
					i+=2
				i=0
				while i<len(list_of_stacks):
					if (address - self._config.REVERSE) in range(list_of_stacks[i],list_of_stacks[i+1]):
						print("String is in the process's allocated Stack !!!")
						print('\n')
						found=True
						break
					i+=2
				i=0
				while i<len(list_of_modules):
					if (address - self._config.REVERSE) in range(list_of_modules[i],list_of_modules[i+1]):
						print("String is in the process's allocated modules !!!")
						print('\n')
						found=True
						break
					i+=2
				if found==False:
					print("String belongs to an unlloacated block !!!")
					print('\n')
				#print('hola', (address - self._config.REVERSE))
				yield (task, address - self._config.REVERSE, hit, scanner.address_space.zread(address - self._config.REVERSE, self._config.SIZE))
    def _scan_kernel_memory(self, addr_space, rules):
        # Find KDBG so we know where kernel memory begins. Do not assume
        # the starting range is 0x80000000 because we may be dealing with
        # an image with the /3GB boot switch. 
        kdbg = tasks.get_kdbg(addr_space)

        start = kdbg.MmSystemRangeStart.dereference_as("Pointer")

        # Modules so we can map addresses to owners
        mods = dict((addr_space.address_mask(mod.DllBase), mod)
                    for mod in modules.lsmod(addr_space))
        mod_addrs = sorted(mods.keys())

        # There are multiple views (GUI sessions) of kernel memory.
        # Since we're scanning virtual memory and not physical, 
        # all sessions must be scanned for full coverage. This 
        # really only has a positive effect if the data you're
        # searching for is in GUI memory. 
        sessions = []
        
        
        for proc in tasks.pslist(addr_space):
            sid = proc.SessionId
            # Skip sessions we've already seen 
            if sid == None or sid in sessions:
                continue

            session_space = proc.get_process_address_space()
            if session_space == None:
                continue
            
            sessions.append(sid)
            scanner = DiscontigYaraScanner(address_space = session_space,
                                           rules = rules)

            for hit, address in scanner.scan(start_offset = start):


                module = tasks.find_module(mods, mod_addrs, addr_space.address_mask(address))
                yield (module, address - self._config.REVERSE, hit, session_space.zread(address - self._config.REVERSE, self._config.SIZE))

    def calculate(self):
        if not has_yara:
            debug.error("Please install Yara from https://plusvic.github.io/yara/")

        addr_space = utils.load_as(self._config)
        rules = self._compile_rules()
        process_mem = self._scan_process_memory(addr_space, rules)
        kernel_mem = self._scan_kernel_memory(addr_space, rules)

        if self._config.ALL:
            for p in process_mem:
            	
                yield p
            for k in kernel_mem:
                yield k
        elif self._config.KERNEL:
            for k in kernel_mem:
                yield k
        else:
            for p in process_mem:
                yield p

    def unified_output(self, data):
        return TreeGrid([("Rule", str),
                       ("Owner", str),
                       ("Address", Address),
                       ("Data", Bytes)],
                        self.generator(data))

    def generator(self, data):
        if self._config.DUMP_DIR and not os.path.isdir(self._config.DUMP_DIR):
            debug.error(self._config.DUMP_DIR + " is not a directory")
        for o, addr, hit, content in data:
            owner = "Owner: (Unknown Kernel Memory)"
            if o == None:
                filename = "kernel.{0:#x}.dmp".format(addr)
            elif o.obj_name == "_EPROCESS":
                owner = "{0}: (Pid {1})".format(o.ImageFileName, o.UniqueProcessId)
                filename = "process.{0:#x}.{1:#x}.dmp".format(o.obj_offset, addr)
            else:
                owner = "{0}".format(o.BaseDllName)
                filename = "kernel.{0:#x}.{1:#x}.dmp".format(o.obj_offset, addr)

            # Dump the data if --dump-dir was supplied
            if self._config.DUMP_DIR:
                path = os.path.join(self._config.DUMP_DIR, filename)
                fh = open(path, "wb")
                fh.write(content)
                fh.close()

            yield (0, [str(hit.rule), owner, Address(addr), Bytes(content)])

    def render_text(self, outfd, data):

        if self._config.DUMP_DIR and not os.path.isdir(self._config.DUMP_DIR):
            debug.error(self._config.DUMP_DIR + " is not a directory")
        
        
        for o, addr, hit, content in data:
            outfd.write("Rule: {0}\n".format(hit.rule))

            
            if o == None:
                outfd.write("Owner: (Unknown Kernel Memory)\n")
                filename = "kernel.{0:#x}.dmp".format(addr)
            elif o.obj_name == "_EPROCESS":
            	
                outfd.write("Owner: Process {0} Pid {1}\n".format(o.ImageFileName,
                    o.UniqueProcessId))
                filename = "process.{0:#x}.{1:#x}.dmp".format(o.obj_offset, addr)
            else:
                outfd.write("Owner: {0}\n".format(o.BaseDllName))
                filename = "kernel.{0:#x}.{1:#x}.dmp".format(o.obj_offset, addr)

            # Dump the data if --dump-dir was supplied
            if self._config.DUMP_DIR:
                path = os.path.join(self._config.DUMP_DIR, filename)
                fh = open(path, "wb")
                fh.write(content)
                fh.close()

            outfd.write("".join(
                ["{0:#010x}  {1:<48}  {2}\n".format(addr + o, h, ''.join(c))
                for o, h, c in utils.Hexdump(content)
                ]))