from collections import OrderedDict
import pathlib
import shlex
import subprocess
from typing import Any, Iterable, Mapping, Optional, TextIO, Union
import weakref

"""Section hierarchy map

Format:
{
    section title: {
        category (0: Parameter, 1: Molecule, 2: System),
        rank (0: Main section, 1: Subsection),
        occurrence (Allowed occurrene, 0: any, 1: only once)
        }
}
"""
SECTION_HIERARCHY = {
    # Parameter sections
    "defaults": {"category": 0, "rank": 0, "occurence": 1},
    "atomtypes": {"category": 0, "rank": 0, "occurence": 0},
    "bondtypes": {"category": 0, "rank": 0, "occurence": 0},
    "pairtypes": {"category": 0, "rank": 0, "occurence": 0},
    "angletypes": {"category": 0, "rank": 0, "occurence": 0},
    "dihedraltypes": {"category": 0, "rank": 0, "occurence": 0},
    "constrainttypes": {"category": 0, "rank": 0, "occurence": 0},
    "nonbonded_params": {"category": 0, "rank": 0, "occurence": 0},
    # Molecule sections
    "moleculetype": {"category": 1, "rank": 0, "occurence": 0},
    "atoms": {"category": 1, "rank": 1, "occurence": 0},
    "bonds": {"category": 1, "rank": 1, "occurence": 0},
    "pairs": {"category": 1, "rank": 1, "occurence": 0},
    "pairs_nb": {"category": 1, "rank": 1, "occurence": 0},
    "angles": {"category": 1, "rank": 1, "occurence": 0},
    "dihedrals": {"category": 1, "rank": 1, "occurence": 0},
    "exclusions": {"category": 1, "rank": 1, "occurence": 0},
    "constraints": {"category": 1, "rank": 1, "occurence": 0},
    "settles": {"category": 1, "rank": 1, "occurence": 0},
    "virtual_sites2": {"category": 1, "rank": 1, "occurence": 0},
    "virtual_sites3": {"category": 1, "rank": 1, "occurence": 0},
    "virtual_sites4": {"category": 1, "rank": 1, "occurence": 0},
    "virtual_sitesn": {"category": 1, "rank": 1, "occurence": 0},
    "position_restraints": {"category": 1, "rank": 1, "occurence": 0},
    "distance_restraints": {"category": 1, "rank": 1, "occurence": 0},
    "dihedral_restraints": {"category": 1, "rank": 1, "occurence": 0},
    "orientation_restraints": {"category": 1, "rank": 1, "occurence": 0},
    "angle_restraints": {"category": 1, "rank": 1, "occurence": 0},
    "angle_restraints_z": {"category": 1, "rank": 1, "occurence": 0},
    # System sections
    "system": {"category": 2, "rank": 0, "occurence": 1},
    "molecules": {"category": 2, "rank": 0, "occurence": 1},
}


class GromacsTop:
    def __init__(self):
        self._nodes = dict()
        self._hardroot = Node()
        self._hardroot.key = self._hardroot.value = None
        self._root = root = weakref.proxy(self._hardroot)
        root.prev = root.next = root

    def __str__(self):
        return_str = ""
        for node in self:
            return_str += f"{node.key} {node.value!s}\n\n"

        return return_str

    def __iter__(self):
        for node in self._nodes.values():
            yield node

    def add(self, key, value):
        self._nodes[key] = node = Node()
        root = self._root
        last = root.prev
        node.prev, node.next, node.key, node.value = last, root, key, value
        last.next = node
        root.prev = weakref.proxy(node)

    def remove(self, key):
        node = self._nodes[key]
        node.prev.next = node.next
        node.next.prev = weakref.proxy(node.prev)
        _ = self._nodes.pop(key)

    @property
    def includes_resolved(self):
        for node in self:
            if isinstance(node.value, Include):
                return False
        else:
            return True

    @property
    def conditions_resolved(self):
        for node in self:
            if isinstance(node.value, Condition):
                return False
        else:
            return True


class GromacsTopParser:
    """Read and write GROMACS topology files"""

    def __init__(
            self,
            ignore_comments: bool = True,
            comment_chars: Optional[Iterable[str]] = None,
            preprocess: bool = True,
            include_local: bool = True,
            include_shared: bool = False,
            local_paths: Optional[Iterable[Any]] = None,
            shared_paths: Optional[Iterable[Any]] = None,
            definitions: Optional[Mapping[str, Any]] = None,
            resolve_conditions: bool = True,
            verbose: bool = True):

        self.ignore_comments = ignore_comments

        if comment_chars is None:
            comment_chars = [";", "*"]
        self.comment_chars = [char for char in comment_chars]

        self.preprocess = preprocess
        self.include_local = include_local

        if local_paths is not None:
            local_paths = [pathlib.Path(p) for p in local_paths]
        self.local_paths = local_paths

        self.include_shared = include_shared

        if shared_paths is not None:
            shared_paths = [pathlib.Path(p) for p in shared_paths]
        self.shared_paths = shared_paths

        self.verbose = verbose
        self.resolve_conditions = resolve_conditions

        self.definitions = {}
        if definitions is not None:
            self.definitions.update(definitions)

    def preprocess_includes(
            self,
            file: Union[TextIO, Iterable[str]],
            include_local=True,
            local_paths=None,
            include_shared=False,
            shared_paths=None):
        """Pre-process topology file-like object

        Yield topology file line by line and resolve '#include'
        statements.

        Args:
            file: File-like iterable.
        """

        if local_paths is None:
            _local_paths = []

            try:
                file_path = pathlib.Path(file.name).parent.absolute()
            except AttributeError:
                file_path = None

            if file_path is not None:
                _local_paths.append(file_path)

        else:
            _local_paths = [pathlib.Path(p) for p in local_paths]

        if shared_paths is None:
            shared_paths = []
            gmx_shared = get_gmx_dir()[1]
            if gmx_shared is not None:
                shared_paths.append(gmx_shared)

        else:
            shared_paths = [pathlib.Path(p) for p in shared_paths]

        for line in file:
            if not line.startswith('#include'):
                yield line
                continue

            include_file = line.split()[1].strip('"')

            found_locally = False
            if include_local:
                for include_dir in _local_paths:
                    include_path = include_dir / include_file
                    if not include_path.is_file():
                        continue

                    with open(include_path) as open_file:
                        yield from self.preprocess_includes(
                            open_file,
                            include_local=include_local,
                            local_paths=local_paths,
                            include_shared=include_shared,
                            shared_paths=shared_paths
                        )
                    found_locally = True
                    break

            found_shared = False
            if not found_locally and include_shared:
                for include_dir in shared_paths:
                    include_path = include_dir / include_file
                    if not include_path.is_file():
                        continue

                    with open(include_path) as open_file:
                        yield from self.preprocess_includes(
                            open_file,
                            include_local=include_local,
                            local_paths=local_paths,
                            include_shared=include_shared,
                            shared_paths=shared_paths
                        )
                    found_shared = True
                    break

            if not (found_locally or found_shared):
                yield line

    def read(self, file: Iterable) -> GromacsTop:
        top = GromacsTop()

        if self.preprocess:
            file = self.preprocess_includes(
                file,
                include_local=self.include_local,
                local_paths=self.local_paths,
                include_shared=self.include_shared,
                shared_paths=self.shared_paths
                )

        comment_chars = tuple(self.comment_chars)

        active_section = "head"
        active_category = 0

        active_conditions = OrderedDict()
        active_definitions = {}
        active_definitions.update(self.definitions)

        previous = ''
        for line in file:

            if line.strip().endswith('\\'):
                # Resolve multi-line statement
                line = line[:line.rfind('\\')]
                previous = f"{previous}{line}"
                continue

            line = f"{previous}{line}"
            previous = ''

            line = line.strip()

            if self.ignore_comments:
                for char in comment_chars:
                    if char not in line:
                        continue

                    line = line[:line.index(char)].strip()

            if line in ['', '\n', '\n\r']:
                continue

            if line.startswith('#define'):
                line = line.lstrip("#define").lstrip().split(maxsplit=1)
                if len(line) == 1:
                    top.add(f"_{len(top._nodes)}", Define(line[0], True))
                    active_definitions[line[0]] = True
                else:
                    top.add(f"_{len(top._nodes)}", Define(line[0], line[1]))
                    active_definitions[line[0]] = line[1]
                continue

            if line.startswith('#undef'):
                line = line.lstrip("#undef").lstrip()
                top.add(f"_{len(top._nodes)}", Define(line, False))
                _ = active_definitions.pop(line)
                continue

            if line.startswith('#ifdef'):
                line = line.lstrip('#ifdef').lstrip()
                active_conditions[line] = True
                top.add(f"_{len(top._nodes)}", Condition(line, True))
                continue

            if line.startswith('#ifndef'):
                line = line.lstrip('#ifndef').lstrip()
                active_conditions[line] = False
                top.add(f"_{len(top._nodes)}", Condition(line, False))
                continue

            if line.startswith('#else'):
                last_condition, last_value = next(
                    reversed(active_conditions.items())
                    )
                active_conditions[last_condition] = not last_value

                top.add(
                    f"_{len(top._nodes)}",
                    Condition(last_condition, None)
                    )
                top.add(
                    f"_{len(top._nodes)}",
                    Condition(last_condition, not last_value)
                    )
                continue

            if line.startswith('#endif'):
                last_condition, _ = active_conditions.popitem(last=True)
                top.add(
                    f"_{len(top._nodes)}",
                    Condition(last_condition, None)
                    )
                continue

            if self.resolve_conditions:
                skip = False
                for condition, required_value in active_conditions.items():
                    defined_value = active_definitions.get(condition, False)
                    if defined_value:
                        # Something truthy?
                        defined_value = True
                    if defined_value is not required_value:
                        skip = True
                        break

                if skip:
                    continue

            if line.startswith(comment_chars):
                char = line[0]
                comment = line[1:].strip()
                top.add(f"_{len(top._nodes)}", Comment(char, comment))
                continue

            if line.startswith("#include"):
                include = line.strip("#include").lstrip()
                top.add(f"_{len(top._nodes)}", Include(include))
                continue

            if line.startswith('['):
                active_section = line.strip(' []').casefold()
                section_info = SECTION_HIERARCHY.get(active_section, None)
                if section_info is None:
                    if self.verbose:
                        print(f"Unknown section {active_section}")
                    top.add(f"_{len(top._nodes)}", Section(active_section))
                    continue

                category = section_info["category"]
                if (category < active_category) and self.verbose:
                    print(f"Inconsistent section {active_section}")
                else:
                    active_category = category

                rank = section_info["rank"]
                if rank == 0:
                    top.add(f"_{len(top._nodes)}", Section(active_section))
                else:
                    top.add(f"_{len(top._nodes)}", Subsection(active_section))

                continue

            if active_section == "defaults":
                args = line.split()
                top.add(f"_{len(top._nodes)}", DefaultsEntry(*args))
                continue

            top.add(f"_{len(top._nodes)}", NodeValue(line))

        return top


class Node:
    __slots__ = ["prev", "next", "key", "value", '__weakref__']


class NodeValue:
    """Generic fallback node value"""
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value.__str__()


class Define:
    """#define or #undef directives"""
    def __init__(self, key, value):
        self.key = key
        self.value = value

    def __str__(self):
        if not isinstance(self.value, bool):
            return f"#define {self.key} {self.value}"

        if self.value is True:
            return f"#define {self.key}"

        if self.value is False:
            return f"#undef {self.key}"


class Condition:
    """#ifdef, #ifndef, #endif directives"""
    def __init__(self, key, value):
        self.key = key
        self.value = value

    def __str__(self):
        if self.value is True:
            return f"#ifdef {self.key}"

        if self.value is False:
            return f"#ifndef {self.key}"

        if self.value is None:
            return "#endif"


class Section:
    """A regular section heading"""
    def __init__(self, title: str):
        self.title = title

    def __str__(self):
        return f"[ {self.title} ]"


class Subsection:
    """A subsection heading"""
    def __init__(self, title: str):
        self.title = title

    def __str__(self):
        return f"[ {self.title} ]"


class Comment:
    """Standalone full-line comment"""
    def __init__(self, char: str, comment: str):
        self.char = char
        self.comment = comment

    def __str__(self):
        return f"{self.char} {self.comment.__str__()}"


class Include:
    """#include directive"""
    def __init__(self, include: str):
        self.include = include

    def __str__(self):
        return f"#include {self.include.__str__()}"


class DefaultsEntry:
    def __init__(
            self, nbfunc, comb_rule,
            gen_pairs="no", fudgeLJ=None, fudgeQQ=None, n=None):

        self.nbfunc = nbfunc
        self.comb_rule = comb_rule
        self.gen_pairs = gen_pairs
        self.fudgeLJ = fudgeLJ
        self.fudgeQQ = fudgeQQ
        self.n = n

    def __str__(self):
        return_str = f"{self.nbfunc:<16}{self.comb_rule:<16}"
        if self.gen_pairs is not None:
            return_str += f"{self.gen_pairs:<16}"
        if self.fudgeLJ is not None:
            return_str += f"{self.fudgeLJ:<8}"
        if self.fudgeQQ is not None:
            return_str += f"{self.fudgeQQ:<8}"
        if self.n is not None:
            return_str += f"{self.n:<8}"
        return return_str


class AtomtypesEntry:
    pass


def get_gmx_dir():
    """Find absolute location of the gromacs shared library files

    This function uses a quick and dirty approach: GROMACS is called and
    stdout is parsed for the entries 'Executable' and 'Data prefix'.
    """

    call = 'gmx -h'
    feedback = subprocess.run(
        shlex.split(call),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding='utf8'
        )

    if feedback.returncode != 0:
        return None, None

    _feedback = feedback.stderr.split('\n')

    gmx_exe = None
    gmx_shared = None

    for line in _feedback:
        if line.startswith('Executable'):
            gmx_exe = pathlib.Path(line.split()[-1])
        if line.startswith('Data prefix'):
            gmx_shared = pathlib.Path(line.split()[-1]) / 'share/gromacs/top'

    return gmx_exe, gmx_shared