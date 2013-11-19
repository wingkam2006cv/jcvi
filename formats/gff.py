#!/usr/bin/env python
# -*- coding: UTF-8 -*-


import sys
import os
import os.path as op
import logging

from collections import defaultdict
from urllib import quote, unquote

from jcvi.formats.base import LineFile, must_open, is_number
from jcvi.formats.fasta import Fasta, SeqIO
from jcvi.formats.bed import Bed, BedLine
from jcvi.utils.iter import flatten
from jcvi.utils.orderedcollections import DefaultOrderedDict, parse_qs
from jcvi.apps.base import OptionParser, OptionGroup, ActionDispatcher, mkdir, \
            need_update, sh


Valid_strands = ('+', '-', '?', '.')
Valid_phases = ('0', '1', '2', '.')
FastaTag = "##FASTA"
RegionTag = "##sequence-region"
valid_gff_parent_child = {"match": "match_part",
                          "cDNA_match": "match_part",
                          "EST_match": "match_part",
                          "nucleotide_to_protein_match": "match_part",
                          "expressed_sequence_match": "match_part",
                          "protein_match": "match_part",
                          "mRNA": "exon"
                         }
valid_gff_type = tuple(valid_gff_parent_child.keys())
reserved_gff_attributes = ("ID", "Name", "Alias", "Parent", "Target",
                           "Gap", "Derives_from", "Note", "Dbxref",
                           "Ontology_term", "Is_circular")
multiple_gff_attributes = ("Parent", "Alias", "Dbxref", "Ontology_term")


class GffLine (object):
    """
    Specification here (http://www.sequenceontology.org/gff3.shtml)
    """
    def __init__(self, sline, key="ID", gff3=True,
                 append_source=False, score_attrib=False):
        args = sline.strip().split("\t")
        self.seqid = args[0]
        self.source = args[1]
        self.type = args[2]
        self.start = int(args[3])
        self.end = int(args[4])
        self.score = args[5]
        self.strand = args[6]
        assert self.strand in Valid_strands, \
                "strand must be one of {0}".format(Valid_strands)
        self.phase = args[7]
        assert self.phase in Valid_phases, \
                "phase must be one of {0}".format(Valid_phases)
        self.attributes_text = "" if len(args) <= 8 else args[8].strip()
        self.attributes = make_attributes(self.attributes_text, gff3=gff3)
        # key is not in the gff3 field, this indicates the conversion to accn
        self.key = key  # usually it's `ID=xxxxx;`
        self.gff3 = gff3

        if append_source and self.key in self.attributes:
            # if `append_source` is True, append the gff `self.source`
            # to `self.key`. use this option to enhance the `self.accn`
            # column in bed file
            self.attributes[self.key][0] = ":".join((self.source, \
                    self.attributes[self.key][0]))

        if score_attrib and score_attrib in self.attributes and is_number(self.attributes[score_attrib][0]):
            # if `score_attrib` is specified, check if it is indeed an
            # attribute or not. If yes, check if the value of attribute
            # is numeric or not. If not, keep original GFF score value
            self.score = self.attributes[score_attrib][0]

    def __getitem__(self, key):
        return getattr(self, key)

    def __str__(self):
        return "\t".join(str(x) for x in (self.seqid, self.source, self.type,
                self.start, self.end, self.score, self.strand, self.phase,
                self.attributes_text))

    def get_attr(self, key, first=True):
        if key in self.attributes.keys():
            if first:
                return self.attributes[key][0]
            return self.attributes[key]
        return None

    def set_attr(self, key, value, update=True, gff3=None, append=False, dbtag=None):
        if type(value) is not list:
            value = [value]
            if key == "Dbxref" and dbtag:
                value = ["{0}:{1}".format(dbtag, x) for x in value]
        if key not in self.attributes.keys() or not append:
            self.attributes[key] = []
        self.attributes[key].extend(value)
        if update:
            self.update_attributes(gff3=gff3, urlquote=False)

    def update_attributes(self, skipEmpty=None, gff3=None, urlquote=True):
        attributes = []
        if gff3 is None:
            gff3 = self.gff3

        sep = ";" if gff3 else "; "
        for tag, val in self.attributes.items():
            if not val and skipEmpty:
                continue
            val = ",".join(val)
            val = "\"{0}\"".format(val) if " " in val and (not gff3) else val
            equal = "=" if gff3 else " "
            if urlquote:
                safechars = " /:?~#+!$'@()*[]|"
                if tag in multiple_gff_attributes:
                    safechars += ","
                val = quote(val, safe=safechars)
            attributes.append(equal.join((tag, val)))

        self.attributes_text = sep.join(attributes)

    def update_tag(self, old_tag, new_tag):
        if old_tag not in self.attributes:
            return
        self.attributes[new_tag] = self.attributes[old_tag]
        del self.attributes[old_tag]

    @property
    def accn(self):
        if self.key and self.key in self.attributes:    # GFF3 format
            a = self.attributes[self.key]
        else:   # GFF2 format
            a = self.attributes_text.split()
        return quote(",".join(a))

    id = accn

    @property
    def span(self):
        return self.end - self.start + 1

    @property
    def bedline(self):
        score = "1000" if self.score == '.' else self.score
        row = "\t".join((self.seqid, str(self.start - 1),
            str(self.end), self.accn, score, self.strand))
        return BedLine(row)


class Gff (LineFile):

    def __init__(self, filename, key="ID", append_source=False, score_attrib=False):
        super(Gff, self).__init__(filename)
        self.key = key
        self.append_source = append_source
        self.score_attrib = score_attrib
        self.gff3 = self.get_gff_type()
        self.fp.seek(0)

    def get_gff_type(self):
        self.gff3 = True
        if self.filename in ("-", "stdin"):
            return True

        # Determine file type
        row = None
        for row in self:
            break
        gff3 = False if not row else "=" in row.attributes_text
        if not gff3:
            logging.debug("File is not gff3 standard.")
        return gff3

    def __iter__(self):
        self.fp = must_open(self.filename)
        for row in self.fp:
            row = row.strip()
            if row.strip() == "":
                continue
            if row[0] == '#':
                if row == FastaTag:
                    break
                continue
            yield GffLine(row, key=self.key, append_source=self.append_source, \
                    score_attrib=self.score_attrib, gff3=self.gff3)

    @property
    def seqids(self):
        return set(x.seqid for x in self)


def make_attributes(s, gff3=True):
    """
    In GFF3, the last column is typically:
    ID=cds00002;Parent=mRNA00002;

    In GFF2, the last column is typically:
    Gene 22240.t000374; Note "Carbonic anhydrase"
    """
    if gff3:
        """
        hack: temporarily replace the '+' sign in the attributes column
        with the string 'PlusSign' to prevent urlparse.parse_qsl() from
        replacing the '+' sign with a space
        """
        s = s.replace('+', 'PlusSign')
        d = parse_qs(s)
        for key in d.iterkeys():
            d[key][0] = unquote(d[key][0].replace('PlusSign', '+'))
    else:
        attributes = s.split(";")
        d = DefaultOrderedDict(list)
        for a in attributes:
            a = a.strip()
            if ' ' not in a:
                continue
            key, val = a.split(' ', 1)
            val = unquote(val.replace('"', '').replace('=', ' ').strip())
            d[key].append(val)

    for key, val in d.items():
        d[key] = list(flatten([v.split(",") for v in val]))

    return d


def main():

    actions = (
        ('bed', 'parse gff and produce bed file for particular feature type'),
        ('bed12', 'produce bed12 file for coding features'),
        ('fromgtf', 'convert gtf to gff3 format'),
        ('gtf', 'convert gff3 to gtf format'),
        ('gb', 'convert gff3 to genbank format'),
        ('sort', 'sort the gff file'),
        ('filter', 'filter the gff file based on Identity and Coverage'),
        ('format', 'format the gff file, change seqid, etc.'),
        ('chain', 'fill in parent features by chaining children'),
        ('rename', 'change the IDs within the gff3'),
        ('uniq', 'remove the redundant gene models'),
        ('liftover', 'adjust gff coordinates based on tile number'),
        ('note', 'extract certain attribute field for each feature'),
        ('load', 'extract the feature (e.g. CDS) sequences and concatenate'),
        ('extract', 'extract contig or features from gff file'),
        ('split', 'split the gff into one contig per file'),
        ('merge', 'merge several gff files into one'),
        ('parents', 'find the parents given a list of IDs'),
        ('children', 'find all children that belongs to the same parent'),
        ('frombed', 'convert from bed format to gff3'),
        ('fromsoap', 'convert from soap format to gff3'),
        ('gapsplit', 'split alignment GFF3 at gaps based on CIGAR string'),
        ('orient', 'orient the coding features based on translation'),
            )

    p = ActionDispatcher(actions)
    p.dispatch(globals())


def gb(args):
    """
    %prog gb gffile fastafile

    Convert GFF3 to Genbank format. Recipe taken from:
    <http://www.biostars.org/p/2492/>
    """
    from Bio import SeqIO
    from Bio.Alphabet import generic_dna
    try:
        from BCBio import GFF
    except ImportError:
        print >> sys.stderr, "You need to install dep first: $ easy_install bcbio-gff"

    p = OptionParser(gb.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    gff_file, fasta_file = args
    pf = op.splitext(gff_file)[0]
    out_file = pf + ".gb"
    fasta_input = SeqIO.to_dict(SeqIO.parse(fasta_file, "fasta", generic_dna))
    gff_iter = GFF.parse(gff_file, fasta_input)
    SeqIO.write(gff_iter, out_file, "genbank")


def orient(args):
    """
    %prog orient in.gff3 features.fasta > out.gff3

    Change the feature orientations based on translation. This script is often
    needed in fixing the strand information after mapping RNA-seq transcripts.

    You can generate the features.fasta similar to this command:

    $ %prog load --parents=EST_match --children=match_part clc.JCVIv4a.gff
    JCVI.Medtr.v4.fasta -o features.fasta
    """
    from jcvi.formats.base import DictFile
    from jcvi.formats.fasta import longestorf

    p = OptionParser(orient.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    ingff3, fastafile = args
    idsfile = fastafile.rsplit(".", 1)[0] + ".orf.ids"
    if need_update(fastafile, idsfile):
        longestorf([fastafile, "--ids"])

    orientations = DictFile(idsfile)
    gff = Gff(ingff3)
    for g in gff:
        id = None
        for tag in ("ID", "Parent"):
            if tag in g.attributes:
                id, = g.attributes[tag]
                break
        assert id

        orientation = orientations.get(id, "+")
        if orientation == '-':
            g.strand = {"+": "-", "-": "+"}[g.strand]

        print g


def rename(args):
    """
    %prog rename in.gff3 switch.ids > reindexed.gff3

    Change the IDs within the gff3.
    """
    from jcvi.formats.base import DictFile

    p = OptionParser(rename.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    ingff3, switch = args
    switch = DictFile(switch)

    gff = Gff(ingff3)
    for g in gff:
        id, = g.attributes["ID"]
        newname = switch.get(id, id)
        g.attributes["ID"] = [newname]

        if "Parent" in g.attributes:
            parents = g.attributes["Parent"]
            g.attributes["Parent"] = [switch.get(x, x) for x in parents]

        g.update_attributes()
        print g


def parents(args):
    """
    %prog parents gffile models.ids

    Find the parents given a list of IDs in "models.ids".
    """
    p = OptionParser(parents.__doc__)

    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    gff_file, idsfile = args
    g = make_index(gff_file)
    fp = open(idsfile)
    for row in fp:
        cid = row.strip()
        b = g.parents(cid, 1).next()
        print "\t".join((cid, b.id))


def filter(args):
    """
    %prog filter gffile > filtered.gff

    Filter the gff file based on Identity and coverage. You can get this type of
    gff by using gmap:

    $ gmap -f 2
    """
    p = OptionParser(filter.__doc__)
    p.add_option("--id", default=95, type="float",
                 help="Minimum identity [default: %default]")
    p.add_option("--coverage", default=90, type="float",
                 help="Minimum coverage [default: %default]")
    p.add_option("--type", default="mRNA",
                 help="The feature to scan for the attributes [default: %default]")
    p.add_option("--nocase", default=False, action="store_true",
                 help="Perform case insensitive lookup of attribute names [default: %default]")

    opts, args = p.parse_args(args)
    otype, oid, ocov = opts.type, opts.id, opts.coverage

    id_attr, cov_attr = "Identity", "Coverage"
    if opts.nocase:
        id_attr, cov_attr = id_attr.lower(), cov_attr.lower()

    if len(args) != 1:
        sys.exit(not p.print_help())

    gffile, = args

    gff = Gff(gffile)
    bad = set()
    relatives = set()
    for g in gff:
        if g.type != otype:
            continue
        identity = float(g.attributes[id_attr][0])
        coverage = float(g.attributes[cov_attr][0])
        if identity < oid or coverage < ocov:
            bad.add(g.accn)
            relatives.add(g.attributes["Parent"][0])

    logging.debug("{0} bad accns marked.".format(len(bad)))

    for g in gff:
        if "Parent" in g.attributes and g.attributes["Parent"][0] in bad:
            relatives.add(g.accn)

    logging.debug("{0} bad relatives marked.".format(len(relatives)))

    for g in gff:
        if g.accn in bad or g.accn in relatives:
            continue
        print g


def fix_gsac(g, notes):
    a = g.attributes

    if g.type == "gene":
        note = a["Name"]
    elif g.type == "mRNA":
        parent = a["Parent"][0]
        note = notes[parent]
    else:
        return

    a["Name"] = a["ID"]
    a["Note"] = note
    g.update_attributes()


def gapsplit(args):
    """
    %prog gapsplit gffile > split.gff

    Read in the gff (normally generated by GMAP) and print it out after splitting
    each feature into one parent and multiple child features based on alignment
    information encoded in CIGAR string.
    """
    import re

    p = OptionParser(gapsplit.__doc__)

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    gffile, = args

    gff = Gff(gffile)
    for g in gff:
        if re.match("EST_match", g.type):
            """
            hacky implementation:
            since the standard urlparse.parse_qsl() replaces all "+" symbols with spaces
            we will write a regex to check either for a "-" or a " " (space)
            """
            match = re.search(r'\S+ (\d+) \d+ ([\s{1}\-])', g.attributes["Target"][0])
            if match.group(2) == "-":
                strand = match.group(2)
            else:
                strand = "+"
                g.attributes["Target"][0] = " ".join(str(x) \
                        for x in [g.attributes["Target"][0].rstrip(), strand])

            if g.strand == "?":
                g.strand = strand
        else:
            match = re.match(r'\S+ (\d+) \d+', g.attributes["Target"][0])
        target_start = int(match.group(1))

        re_cigar = re.compile(r'(\D+)(\d+)');
        cigar = g.attributes["Gap"][0].split(" ")
        g.attributes["Gap"] = None

        parts = []
        if g.strand == "+":
            for event in cigar:
                match = re_cigar.match(event)
                op, count = match.group(1), int(match.group(2))
                if op in "IHS":
                    target_start += count
                elif op in "DN":
                    g.start += count
                elif op == "P":
                    continue
                else:
                    parts.append([g.start, g.start + count - 1, \
                            target_start, target_start + count - 1])
                    g.start += count
                    target_start += count
        else:
            for event in cigar:
                match = re_cigar.match(event)
                op, count = match.group(1), int(match.group(2))
                if op in "IHS":
                    target_start += count
                elif op in "DN":
                    g.end -= count
                elif op == "P":
                    continue
                else:
                    parts.append([g.end - count + 1, g.end, \
                            target_start, target_start + count - 1])
                    g.end -= count
                    target_start += count

        g.update_attributes(skipEmpty=True, gff3=True)
        print g

        parent = g.attributes["Name"][0]
        g.type = "match_part"
        g.attributes.clear()

        for part in parts:
            g.start, g.end = part[0], part[1]
            g.score, g.strand, g.phase = ".", g.strand, "."

            if re.match("EST", g.type):
                target_list = [parent, part[2], part[3], g.strand]
            else:
                target_list = [parent, part[2], part[3]]
            target = " ".join(str(x) for x in target_list)

            g.attributes["Parent"] = [parent]
            g.attributes["Target"] = [target]

            g.update_attributes(skipEmpty=True, gff3=True)
            print g


def chain(args):
    """
    %prog chain gffile > chained.gff

    Fill in parent features by chaining child features and return extent of the
    child coordinates.
    """
    from jcvi.utils.range import range_minmax
    p = OptionParser(chain.__doc__)
    p.set_outfile()

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    gffile, = args
    gffdict = {}
    fw = must_open(opts.outfile, "w")
    gff = Gff(gffile)
    for g in gff:
        id = g.accn
        if id not in gffdict:
            gffdict[id] = { 'seqid': g.seqid,
                            'source': g.source,
                            'strand': g.strand,
                            'type': g.type,
                            'coords': [],
                            'children': [],
                          }

        gffdict[id]['coords'].append((g.start, g.end))

        g.type = valid_gff_parent_child[g.type]
        g.attributes["Parent"] = g.attributes.pop("ID")
        g.attributes["ID"] = ["{0}-{1}".\
                format(id, len(gffdict[id]['children']) + 1)]
        g.update_attributes()
        gffdict[id]['children'].append(g)

    for key, v in sorted(gffdict.items()):
        seqid = v['seqid']
        source = v['source']
        type = v['type']
        strand = v['strand']
        start, stop = range_minmax(gffdict[key]['coords'])
        print >> fw, "\t".join(str(x) for x in [seqid, source, type, start, stop,
            ".", strand, ".", "ID=" + key])
        for child in gffdict[key]['children']:
            print >> fw, child

    fw.close()


def format(args):
    """
    %prog format gffile > formatted.gff

    Read in the gff and print it out, changing seqid, etc.
    """
    from jcvi.formats.base import DictFile
    from jcvi.utils.range import range_minmax
    from jcvi.utils.cbook import AutoVivification
    from jcvi.formats.obo import load_GODag, validate_term

    p = OptionParser(format.__doc__)
    p.add_option("--unique", default=False, action="store_true",
                 help="Make IDs unique [default: %default]")
    p.add_option("--gff3", default=False, action="store_true",
                 help="Force to write gff3 attributes [default: %default]")
    p.add_option("--name", help="Add Name from two-column file [default: %default]")
    p.add_option("--note", help="Add Note from two-column file [default: %default]")
    p.add_option("--seqid", help="Switch seqid from two-column file [default: %default]")
    p.add_option("--source", help="Switch GFF source from two-column file. If not" +
                " a file, value will globally replace GFF source [default: %default]")
    p.add_option("--multiparents", default=False, action="store_true",
                 help="Separate features with multiple parents [default: %default]")
    p.add_option("--chaindup", default=None, dest="duptype",
                 help="Chain duplicate features of a particular GFF3 `type`," + \
                      " sharing the same ID attribute [default: %default]")
    p.add_option("--gsac", default=False, action="store_true",
                 help="Fix GSAC GFF3 file attributes [default: %default]")
    p.add_option("--fixphase", default=False, action="store_true",
                 help="Change phase 1<->2, 2<->1 [default: %default]")
    p.add_option("--add_attribute", dest="attrib_files", help="Add new attribute(s) " +
                "from two-column file(s); accepts comma-separated list of files; " +
                "attribute name comes from filename [default: %default]")
    p.add_option("--add_dbxref", dest="dbxref_files", help="Add new Dbxref value(s) (DBTAG:ID) " + \
                "from two-column file(s). DBTAG comes from filename, ID comes from 2nd column; " + \
                "accepts comma-separated list of files; [default: %default]")
    p.add_option("--remove_feats", help="Comma separated list of features to remove" + \
                " [default: %default]")
    p.set_outfile()
    p.add_option("--nostrict", default=False, action="store_true",
                 help="Disable strict parsing of mapping file [default: %default]")
    p.set_SO_opts()

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    gffile, = args
    mapfile = opts.seqid
    names = opts.name
    note = opts.note
    source = opts.source
    attrib_files = opts.attrib_files
    dbxref_files = opts.dbxref_files
    gsac = opts.gsac
    if opts.unique and opts.duptype:
        logging.debug("Cannot use `--unique` and `--chaindup` together")
        sys.exit()
    unique = opts.unique
    duptype = opts.duptype
    fixphase = opts.fixphase
    phaseT = {"1":"2", "2":"1"}
    remove_feats = opts.remove_feats.split(",") if opts.remove_feats else None
    strict = False if opts.nostrict else True

    outfile = opts.outfile

    if mapfile:
        mapping = DictFile(mapfile, delimiter="\t", strict=strict)
    if note:
        note = DictFile(note, delimiter="\t", strict=strict)
    if source and op.isfile(source):
        source = DictFile(source, delimiter="\t", strict=strict)
    if names:
        names = DictFile(names, delimiter="\t", strict=strict)
    if attrib_files:
        attr_values, files = {}, attrib_files.split(",")
        for fn in files:
            attr_name = op.basename(fn).rsplit(".", 1)[0]
            if attr_name not in reserved_gff_attributes:
                attr_name = attr_name.lower()
            attr_values[attr_name] = DictFile(fn, delimiter="\t", strict=strict)
    if dbxref_files:
        dbxref_values, files = {}, dbxref_files.split(",")
        for fn in files:
            dbtag = op.basename(fn).rsplit(".", 1)[0]
            dbxref_values[dbtag] = DictFile(fn, delimiter="\t", strict=strict)

    if gsac:  # setting gsac will force IDs to be unique
        unique = True
        notes = {}

    if unique or duptype or remove_feats:
        if unique:
            dupcounts = defaultdict(int)
            seen = defaultdict(int)
            newparentid = {}
        elif duptype:
            dupranges = AutoVivification()
            skip = defaultdict(int)
        if remove_feats:
            remove = set()
        gff = Gff(gffile)
        for idx, g in enumerate(gff):
            if opts.gff3 and "ID" not in g.attributes.keys():
                id = "{0}_{1}".format(str(g.type).lower(), idx)
            else:
                id = g.accn
            if unique:
                dupcounts[id] += 1
            elif duptype and g.type == duptype:
                dupranges[id][idx] = (g.start, g.end)
            if remove_feats and g.type in remove_feats:
                remove.add(id)

    if opts.verifySO:
        so = load_GODag()

    fw = must_open(outfile, "w")
    gff = Gff(gffile)
    for idx, g in enumerate(gff):
        if remove_feats:
            if g.type in remove_feats:
                id = g.get_attr("ID")
                if id in remove:
                    continue
            else:
                if "Parent" in g.attributes.keys():
                    keep, parent = [], g.get_attr("Parent", first=False)
                    for i, pid in enumerate(parent):
                        if pid not in remove:
                            keep.append(parent[i])
                    if len(keep) == 0:
                        continue
                    parent = g.set_attr("Parent", keep)

        if opts.verifySO:
            ntype = validate_term(g.type, so=so, method=opts.verifySO)
            if ntype and g.type != ntype:
                logging.debug("Resolved term to `{0}`".format(ntype))
                g.type = ntype

        origid = g.seqid
        if fixphase:
            phase = g.phase
            g.phase = phaseT.get(phase, phase)

        if mapfile:
            if origid in mapping:
                g.seqid = mapping[origid]
            else:
                logging.error("{0} not found in `{1}`. ID unchanged.".\
                        format(origid, mapfile))

        if source:
            if isinstance(source, dict) and g.source in source:
                g.source = source[g.source]
            else:
                g.source = source

        id = g.get_attr("ID")
        if names:
            if id in names:
                g.set_attr("Name", names[id])

        if note:
            name = g.get_attr("Name")
            tag = None
            if id in note:
                tag = note[id]
            elif name and name in note:
                tag = note[name]

            if tag:
                g.set_attr("Note", tag)

        if attrib_files:
            for attr_name in attr_values.keys():
                if id in attr_values[attr_name].keys():
                    g.set_attr(attr_name, attr_values[attr_name][id])

        if dbxref_files:
            for dbtag in dbxref_values.keys():
                if id in dbxref_values[dbtag].keys():
                    g.set_attr("Dbxref", dbxref_values[dbtag][id], dbtag=dbtag, append=True)

        if unique:
            if opts.gff3 and "ID" not in g.attributes.keys():
                g.set_attr("ID", "{0}_{1}".format(str(g.type).lower(), idx))

            id = g.accn
            if dupcounts[id] > 1:
                seen[id] += 1
                old_id = id
                id = "{0}-{1}".format(old_id, seen[old_id])
                newparentid[old_id] = id
                g.set_attr("ID", id, gff3=True)

            if "Parent" in g.attributes.keys():
                parent = g.attributes["Parent"][0]
                if dupcounts[parent] > 1:
                    g.set_attr("Parent", newparentid[parent], gff3=True)

        if duptype:
            id = g.accn
            if duptype == g.type and len(dupranges[id]) > 1:
                p = sorted(dupranges[id].keys())
                s, e = dupranges[id][p[0]][0:2]  # get coords of first encountered feature
                if g.start == s and g.end == e and p[0] == idx:
                    r = [dupranges[id][x] for x in dupranges[id].keys()]
                    g.start, g.end = range_minmax(r)
                else:
                    skip[(idx, id, g.start, g.end)] = 1

        if gsac and g.type == "gene":
            notes[g.accn] = g.attributes["Name"]

        pp = g.attributes.get("Parent", [])
        if opts.multiparents and len(pp) > 1:  # separate multiple parents
            id = g.get_attr("ID")
            for i, parent in enumerate(pp):
                g.set_attr("ID", "{0}-{1}".format(id, i + 1), update=False)
                g.set_attr("Parent", parent)
                if gsac:
                    fix_gsac(g, notes)
                print >> fw, g
        else:
            if opts.gff3:
                g.update_attributes(gff3=True)
            if gsac:
                fix_gsac(g, notes)
            if duptype == g.type and skip[(idx, g.accn, g.start, g.end)] == 1:
                continue
            print >> fw, g

    fw.close()


def liftover(args):
    """
    %prog liftover gffile > liftover.gff

    Adjust gff coordinates based on tile number. For example,
    "gannotation.asmbl.000095.7" is the 8-th tile on asmbl.000095.
    """
    p = OptionParser(liftover.__doc__)
    p.add_option("--tilesize", default=50000, type="int",
                 help="The size for each tile [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    gffile, = args
    gff = Gff(gffile)
    for g in gff:
        seqid = g.seqid
        seqid, tilenum = seqid.rsplit(".", 1)
        tilenum = int(tilenum)
        g.seqid = seqid
        offset = tilenum * opts.tilesize
        g.start += offset
        g.end += offset
        print g


def get_piles(allgenes):
    """
    Before running uniq, we need to compute all the piles. The piles are a set
    of redundant features we want to get rid of. Input are a list of GffLines
    features. Output are list of list of features distinct "piles".
    """
    from jcvi.utils.range import Range, range_piles

    ranges = [Range(a.seqid, a.start, a.end, 0, i) \
                    for i, a in enumerate(allgenes)]

    for pile in range_piles(ranges):
        yield [allgenes[x] for x in pile]


def match_span(f1, f2):
    return (f1.start == f2.start) and (f1.stop == f2.stop)


def match_ftype(f1, f2):
    return f1.featuretype == f2.featuretype


def match_nchildren(f1c, f2c):
    return len(list(f1c)) == len(list(f2c))


def match_child_ftype(f1c, f2c):
    from collections import Counter

    return len(set(Counter(i.featuretype for i in f1c).keys()) ^ \
            set(Counter(i.featuretype for i in f2c).keys()))


def match_feats(f1, f2, gffdb, iter):
    """
    Given 2 gffutils database features, compare the features against each other
    to identify if gene structures are the same or different
    """
    if match_span(f1, f2):
        for n in range(1, iter + 1):
            f1c, f2c = gffdb.children(f1, level=n), gffdb.children(f2, level=n)
            if match_child_ftype(f1c, f2c) == 0:
                if match_nchildren(f1c, f2c):
                    for cf1, cf2 in zip(f1c, f2c):
                        if not match_span(cf1, cf2):
                            return False
                else:
                    return False
            else:
                return False
    else:
        return False

    return True


def dedup_pile(newgrp, group, gffdb, iter):
    """
    Identify all redundant gene structures and remove all but one duplicate
    entity from the pile (which has already been filtered by span/score)

    Performs all possible pairwise comparisons of gene structure within the pile
    """
    from itertools import combinations
    from jcvi.utils.grouper import Grouper

    pile = {}
    dups = Grouper()
    for elem in group:
        pile[elem.accn] = elem

    for f1, f2 in combinations(pile.keys(), 2):
        dbf1, dbf2 = gffdb[f1], gffdb[f2]
        if match_feats(dbf1, dbf2, gffdb, iter):
            dups.join(f1, f2)
        else:
            for f in (f1, f2):
                if f not in dups:
                    newgrp[f] = 1
                elif f in newgrp:
                    newgrp.pop(f, None)

    for dup in dups:
        scores = []
        for d in dup:
            for x in (elem for elem in group if elem.accn == d):
                scores.append((- float(x.score), x))

        scores.sort()
        (bscore, best) = scores[0]
        newgrp[best.accn] = 1


def uniq(args):
    """
    %prog uniq gffile > uniq.gff

    Remove redundant gene models. For overlapping gene models, take the longest
    gene. A second scan takes only the genes selected.

    --mode controls whether you want larger feature, or higher scoring feature.
    --best controls how many redundant features to keep, e.g. 10 for est2genome.
    """
    supported_modes = ("span", "score")
    p = OptionParser(uniq.__doc__)
    p.add_option("--type", default="gene",
                 help="Types of features to non-redundify [default: %default]")
    p.add_option("--mode", default="span", choices=supported_modes,
                 help="Pile mode [default: %default]")
    p.add_option("--best", default=1, type="int",
                 help="Use best N features [default: %default]")
    p.add_option("--name", default=False, action="store_true",
                 help="Non-redundify Name attribute [default: %default]")
    p.add_option("--dedup", default=False, action="store_true",
                 help="Iterate through every pile and remove all but one feature " + \
                      "within a group of features sharing gene structure " + \
                      "[default: %default]")
    p.add_option("--iter", default="2", choices=("1", "2"),
                 help="Number of iterations to grab children [default: %default]")
    p.set_cpus()
    p.set_outfile()

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    gffile, = args
    gff = Gff(gffile)
    mode = opts.mode
    bestn = opts.best
    type = opts.type
    allgenes = []
    for g in gff:
        if g.type != type:
            continue
        allgenes.append(g)

    logging.debug("A total of `{0}` {1} features imported.".format(len(allgenes), type))
    allgenes.sort(key=lambda x: (x.seqid, x.start))

    g = get_piles(allgenes)

    bestids, flt_groups = set(), []
    for group in g:
        if mode == "span":
            scores_group = [(- x.span, x) for x in group]
        else:
            scores_group = [(- float(x.score), x) for x in group]

        flt_group = []
        scores_group.sort()
        seen = set()
        for score, x in scores_group:
            if len(seen) >= bestn:
                break

            name = x.attributes["Name"][0] if opts.name else x.accn
            if name in seen:
                continue

            seen.add(name)
            flt_group.append(x) if opts.dedup \
                    else bestids.add(x.accn)

        flt_groups.append(flt_group)

    if opts.dedup:
        gffdb = make_index(gffile)

        from jcvi.utils.iter import grouper
        from jcvi.apps.grid import Jobs
        from multiprocessing import Manager

        manager = Manager()
        results = manager.dict()

        logging.debug("Deduplicating `{0}` piles at a time".format(opts.cpus))
        for cpu_groups in grouper(opts.cpus, flt_groups):
            jobs = Jobs(dedup_pile, [(results, flt_group, gffdb, int(opts.iter)) \
                    for flt_group in cpu_groups])
            jobs.run()
        logging.debug("Deduplication complete".format(len(flt_groups)))

        for x in results.keys():
            bestids.add(x)

    populate_children(opts.outfile, bestids, gffile, opts.type, iter=opts.iter)


def populate_children(outfile, ids, gffile, otype, iter="2"):
    fw = must_open(outfile, "w")
    logging.debug("A total of `{0}` {1} features selected.".format(len(ids), otype))
    logging.debug("Populate children. Iteration 1..")
    gff = Gff(gffile)
    children = set()
    for g in gff:
        if "Parent" not in g.attributes:
            continue
        for parent in g.attributes["Parent"]:
            if parent in ids:
                children.add(g.accn)

    if iter == "2":
        logging.debug("Populate children. Iteration 2..")
        gff = Gff(gffile)
        for g in gff:
            if "Parent" not in g.attributes:
                continue
            for parent in g.attributes["Parent"]:
                if parent in children:
                    children.add(g.accn)

    logging.debug("Filter gff file..")
    gff = Gff(gffile)
    seen = set()
    for g in gff:
        accn = g.accn
        if accn in seen:
            continue
        if (g.type == otype and accn in ids) or (accn in children):
            seen.add(accn)
            print >> fw, g
    fw.close()


def sort(args):
    """
    %prog sort gffile

    Sort gff file.
    """
    p = OptionParser(sort.__doc__)
    p.add_option("-i", dest="inplace", default=False, action="store_true",
                 help="Sort inplace [default: %default]")
    p.set_tmpdir()
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    gffile, = args
    sortedgff = op.basename(gffile).rsplit(".", 1)[0] + ".sorted.gff"
    if opts.inplace:
        sortedgff = gffile

    cmd = "sort"
    if opts.tmpdir:
        cmd += " -T {0}".format(opts.tmpdir)
    cmd += " -k1,1 -k4,4n {0} -o {1}".format(gffile, sortedgff)
    sh(cmd)


def fromgtf(args):
    """
    %prog fromgtf gtffile

    Convert gtf to gff file. In gtf, the "transcript_id" will convert to "ID=",
    the "transcript_id" in exon/CDS feature will be converted to "Parent=".
    """
    p = OptionParser(fromgtf.__doc__)
    p.add_option("--transcript_id", default="transcript_id",
                 help="Field name for transcript [default: %default]")
    p.add_option("--gene_id", default="gene_id",
                 help="Field name for gene [default: %default]")
    p.add_option("--augustus", default=False, action="store_true",
                 help="Input is AUGUSTUS gtf [default: %default]")
    p.set_home("augustus")
    p.set_outfile()
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    gtffile, = args
    outfile = opts.outfile
    if opts.augustus:
        ahome = opts.augustus_home
        s = op.join(ahome, "scripts/gtf2gff.pl")
        cmd = "{0} --gff3 < {1} --out={2}".format(s, gtffile, outfile)
        sh(cmd)
        return

    gff = Gff(gtffile)
    fw = must_open(outfile, "w")
    transcript_id = opts.transcript_id
    gene_id = opts.gene_id
    nfeats = 0
    for g in gff:
        if g.type in ("transcript", "mRNA"):
            g.type = "mRNA"
            g.update_tag(transcript_id, "ID")
            g.update_tag("mRNA", "ID")
            g.update_tag(gene_id, "Parent")
            g.update_tag("Gene", "Parent")
        elif g.type in ("exon", "CDS") or "UTR" in g.type:
            g.update_tag("transcript_id", "Parent")
            g.update_tag(g.type, "Parent")
        elif g.type == "gene":
            g.update_tag(gene_id, "ID")
            g.update_tag("Gene", "ID")
        else:
            assert 0, "Don't know how to deal with {0}".format(g.type)

        g.update_attributes(gff3=True)
        print >> fw, g
        nfeats += 1

    logging.debug("A total of {0} features written.".format(nfeats))


def frombed(args):
    """
    %prog frombed bed_file [--options] > gff_file

    Convert bed to gff file. In bed, the accn will convert to key='ID'
    Default type will be `match` and default source will be `source`
    """
    p = OptionParser(frombed.__doc__)
    p.add_option("--type", default="match",
                 help="GFF feature type [default: %default]")
    p.add_option("--source", default="default",
                help="GFF source qualifier [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    bedfile, = args
    bed = Bed(bedfile)

    for b in bed:
        print b.gffline(type=opts.type, source=opts.source)


def fromsoap(args):
    """
    %prog fromsoap soapfile > gff_file

    """
    p = OptionParser(fromsoap.__doc__)
    p.add_option("--type", default="nucleotide_match",
                 help="GFF feature type [default: %default]")
    p.add_option("--source", default="soap",
                help="GFF source qualifier [default: %default]")
    p.set_fixchrnames(orgn="maize")
    p.set_outfile()
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    soapfile, = args
    pad0 = len(str(sum(1 for line in open(soapfile))))

    fw = must_open(opts.outfile, "w")
    fp = must_open(soapfile)
    for idx, line in enumerate(fp):
        if opts.fix_chr_name:
            from jcvi.utils.cbook import fixChromName
            line = fixChromName(line, orgn=opts.fix_chr_name)

        atoms = line.strip().split("\t")
        attributes = "ID=match{0};Name={1}".format(str(idx).zfill(pad0), atoms[0])
        start, end = int(atoms[8]), int(atoms[5]) + int(atoms[8]) - 1
        seqid = atoms[7]

        print >> fw, "\t".join(str(x) for x in (seqid, opts.source, opts.type, \
            start, end, ".", atoms[6], ".", attributes))


def gtf(args):
    """
    %prog gtf gffile

    Convert gff to gtf file. In gtf, only exon/CDS features are important. The
    first 8 columns are the same as gff, but in the attributes field, we need to
    specify "gene_id" and "transcript_id".
    """
    p = OptionParser(gtf.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    gffile, = args
    gff = Gff(gffile)
    transcript_to_gene = {}
    for g in gff:
        if g.type == "mRNA":
            if "ID" in g.attributes and "Parent" in g.attributes:
                transcript_id = g.attributes["ID"][0]
                gene_id = g.attributes["Parent"][0]
            elif "mRNA" in g.attributes and "Gene" in g.attributes:
                transcript_id = g.attributes["mRNA"][0]
                gene_id = g.attributes["Gene"][0]
            else:
                transcript_id = g.attributes["ID"][0]
                gene_id = transcript_id
            transcript_to_gene[transcript_id] = gene_id
            continue

        if g.type not in ("CDS", "exon", "start_codon", "stop_codon"):
            continue

        try:
            transcript_id = g.attributes["Parent"]
        except IndexError:
            transcript_id = g.attributes["mRNA"]

        for tid in transcript_id:
            gene_id = transcript_to_gene[tid]
            g.attributes = dict(gene_id=[gene_id], transcript_id=[tid])
            g.update_attributes()

            print g


def merge(args):
    """
    %prog merge gffiles

    Merge several gff files into one. When only one file is given, it is assumed
    to be a file with a list of gff files.
    """
    p = OptionParser(merge.__doc__)
    p.set_outfile()

    opts, args = p.parse_args(args)

    nargs = len(args)
    if nargs < 1:
        sys.exit(not p.print_help())

    if nargs == 1:
        listfile, = args
        fp = open(listfile)
        gffiles = [x.strip() for x in fp]
    else:
        gffiles = args

    outfile = opts.outfile

    deflines = set()
    fw = must_open(outfile, "w")
    fastarecs = {}
    for gffile in gffiles:
        fp = open(gffile)
        for row in fp:
            row = row.rstrip()
            if not row or row[0] == '#':
                if row == FastaTag:
                    break
                if row in deflines:
                    continue
                else:
                    deflines.add(row)

            print >> fw, row

        f = Fasta(gffile, lazy=True)
        for key, rec in f.iteritems_ordered():
            if key in fastarecs.keys():
                continue
            fastarecs[key] = rec

    print >> fw, FastaTag
    SeqIO.write(fastarecs.values(), fw, "fasta")


def extract(args):
    """
    %prog extract gffile

    --contigs: Extract particular contig(s) from the gff file. If multiple contigs are
    involved, use "," to separate, e.g. "contig_12,contig_150"
    --names: Provide a file with IDs, one each line
    """
    p = OptionParser(extract.__doc__)
    p.add_option("--contigs",
                help="Extract features from certain contigs [default: %default]")
    p.add_option("--names",
                help="Extract features with certain names [default: %default]")
    p.add_option("--children", default=False, action="store_true",
                help="Grab children and grand children [default: %default]")
    p.add_option("--tag", default="ID",
                help="Scan the tags for the names [default: %default]")
    p.add_option("--fasta", default=False, action="store_true",
                help="Write FASTA if available [default: %default]")
    p.set_outfile()

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    gffile, = args
    contigID = opts.contigs
    namesfile = opts.names
    nametag = opts.tag

    contigID = set(contigID.split(",")) if contigID else None
    names = set(x.strip() for x in open(namesfile)) if namesfile else None
    outfile = opts.outfile
    if opts.children:
        assert names is not None, "Must set --names"
        populate_children(outfile, names, gffile, "gene")
        return

    fp = open(gffile)
    for row in fp:
        atoms = row.split()
        if len(atoms) == 0:
            continue
        tag = atoms[0]
        if row[0] == "#":
            if row.strip() == "###":
                continue
            if not (tag == RegionTag and contigID and atoms[1] not in contigID):
                print >> fw, row.rstrip()
            if tag == FastaTag:
                break
            continue

        b = GffLine(row)
        attrib = b.attributes
        if contigID and tag not in contigID:
            continue
        if names:
            if nametag not in attrib:
                continue
            if attrib[nametag][0] not in names:
                continue

        print >> fw, row.rstrip()

    if not opts.fasta:
        return

    f = Fasta(gffile)
    for s in contigID:
        if s in f:
            SeqIO.write([f[s]], fw, "fasta")


def split(args):
    """
    %prog split gffile outdir

    Split the gff into one contig per file. Will also take sequences if the file
    contains FASTA sequences.
    """
    p = OptionParser(split.__doc__)

    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    gffile, outdir = args
    mkdir(outdir)

    g = Gff(gffile)
    seqids = g.seqids

    for s in seqids:
        outfile = op.join(outdir, s + ".gff")
        extract([gffile, "--contigs=" + s, "--outfile=" + outfile])


def note(args):
    """
    %prog note gffile > tabfile

    Extract certain attribute field for each feature.
    """
    p = OptionParser(note.__doc__)
    p.add_option("--key", default="Parent",
            help="The key field to extract [default: %default]")
    p.add_option("--attribute", default="Note",
            help="The attribute field to extract [default: %default]")

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    gffile, = args
    key = opts.key
    attrib = opts.attribute

    gff = Gff(gffile)
    seen = set()
    for g in gff:
        if attrib in g.attributes:
            keyval = (g.attributes[key][0], g.attributes[attrib][0])
            if keyval not in seen:
                print "\t".join(keyval)
                seen.add(keyval)


def bed(args):
    '''
    %prog bed gff_file [--options]

    Parses the start, stop locations of the selected features out of GFF and
    generate a bed file
    '''
    p = OptionParser(bed.__doc__)
    p.add_option("--type", dest="type", default="gene",
            help="Feature type to extract, use comma for multiple [default: %default]")
    p.add_option("--key", dest="key", default="ID",
            help="Key in the attributes to extract [default: %default]")
    p.add_option("--source",
            help="Source to extract from, use comma for multiple [default: %default]")
    p.add_option("--score_attrib", dest="score_attrib", default=False,
            help="Attribute whose value is to be used as score in `bedline` [default: %default]")
    p.add_option("--append_source", default=False, action="store_true",
            help="Append GFF source name to extracted key value")
    p.add_option("--nosort", default=False, action="store_true",
            help="Do not sort the output bed file [default: %default]")
    p.set_outfile()

    opts, args = p.parse_args(args)
    if len(args) != 1:
        sys.exit(not p.print_help())

    gffile, = args
    key = opts.key
    if key == "None":
        key = None

    type = set(x.strip() for x in opts.type.split(","))
    source = set()
    if opts.source:
        source = set(x.strip() for x in opts.source.split(","))

    gff = Gff(gffile, key=key, append_source=opts.append_source, score_attrib=opts.score_attrib)
    b = Bed()

    for g in gff:
        if g.type not in type or (source and g.source not in source):
            continue

        b.append(g.bedline)

    sorted = not opts.nosort
    b.print_to_file(opts.outfile, sorted=sorted)


def make_index(gff_file):
    """
    Make a sqlite database for fast retrieval of features.
    """
    import gffutils
    db_file = gff_file + ".db"

    if need_update(gff_file, db_file):
        if op.exists(db_file):
            os.remove(db_file)
        gffutils.create_db(gff_file, db_file)

    return gffutils.FeatureDB(db_file)


def get_parents(gff_file, parents):
    gff = Gff(gff_file)
    for g in gff:
        if g.type not in parents:
            continue
        yield g


def children(args):
    """
    %prog children gff_file

    Get the children that have the same parent.
    """
    p = OptionParser(children.__doc__)
    p.add_option("--parents", default="gene",
            help="list of features to extract, use comma to separate (e.g."
            "'gene,mRNA') [default: %default]")

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    gff_file, = args
    g = make_index(gff_file)
    parents = set(opts.parents.split(','))

    for feat in get_parents(gff_file, parents):

        cc = [c.id for c in g.children(feat.id, 1)]
        if len(cc) <= 1:
            continue

        print "\t".join(str(x) for x in \
                    (feat.id, feat.start, feat.stop, "|".join(cc)))


def load(args):
    '''
    %prog load gff_file fasta_file [--options]

    Parses the selected features out of GFF, with subfeatures concatenated.
    For example, to get the CDS sequences, do this:
    $ %prog load athaliana.gff athaliana.fa --parents mRNA --children CDS

    To get 500bp upstream of a genes Transcription Start Site (TSS), do this:
    $ %prog load athaliana.gff athaliana.fa --feature=upstream:TSS:500

    Switch TSS with TrSS for Translation Start Site.
    '''
    from datetime import datetime as dt
    from jcvi.formats.fasta import Seq, SeqRecord

    # can request output fasta sequence id to be picked from following attributes
    valid_id_attributes = ["ID", "Name", "Parent", "Alias", "Target"]

    p = OptionParser(load.__doc__)
    p.add_option("--parents", dest="parents", default="mRNA",
            help="list of features to extract, use comma to separate (e.g." + \
            "'gene,mRNA') [default: %default]")
    p.add_option("--children", dest="children", default="CDS",
            help="list of features to extract, use comma to separate (e.g." + \
            "'five_prime_UTR,CDS,three_prime_UTR') [default: %default]")
    p.add_option("--feature", dest="feature",
            help="feature type to extract. e.g. `--feature=CDS` or " + \
            "`--feature=upstream:TSS:500` [default: %default]")
    p.add_option("--id_attribute", choices=valid_id_attributes,
            help="The attribute field to extract and use as FASTA sequence ID " + \
            "[default: %default]")
    p.add_option("--desc_attribute",
            help="The attribute field to extract and use as FASTA sequence " + \
            "description [default: %default]")
    p.add_option("--full_header", dest="full_header", default=False, action="store_true",
            help="Specify if full FASTA header (with seqid, coordinates and datestamp)" + \
            " should be generated [default: %default]")

    g1 = OptionGroup(p, "Optional parameters (if generating full header)")
    g1.add_option("--sep", dest="sep", default=" ", \
            help="Specify separator used to delimiter header elements [default: \"%default\"]")
    g1.add_option("--datestamp", dest="datestamp", \
            help="Specify a datestamp in the format YYYYMMDD or automatically pick `today`" + \
            " [default: %default]")
    g1.add_option("--conf_class", dest="conf_class", default=False, action="store_true",
            help="Specify if `conf_class` attribute should be parsed and placed in the header" + \
            " [default: %default]")
    p.add_option_group(g1)

    p.set_outfile()

    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(p.print_help())

    gff_file, fasta_file = args

    if opts.feature:
        opts.feature, opts.parent, opts.children, upstream_site, upstream_len, \
                flag, error_msg = parse_feature_param(opts.feature)
        if flag:
            sys.exit(error_msg)

    parents = set(opts.parents.split(','))
    children_list = set(opts.children.split(','))
    id_attr = opts.id_attribute
    desc_attr = opts.desc_attribute
    sep = opts.sep

    g = make_index(gff_file)
    f = Fasta(fasta_file, index=False)
    seqlen = {}
    for seqid, size in f.itersizes():
        seqlen[seqid] = size

    fw = must_open(opts.outfile, "w")

    for feat in get_parents(gff_file, parents):
        desc = ",".join(feat.attributes[desc_attr]) \
                if desc_attr and desc_attr in feat.attributes else ""

        if opts.full_header:
            desc_parts = []
            desc_parts.append(desc)

            if opts.conf_class and 'conf_class' in feat.attributes:
                desc_parts.append(feat.attributes['conf_class'][0])

            (s, e) = (feat.start, feat.end) if (feat.strand == "+") \
                    else (feat.end, feat.start)
            feat_coords = "{0}:{1}-{2}".format(feat.seqid, s, e)
            desc_parts.append(feat_coords)

            datestamp = opts.datestamp if opts.datestamp else \
                    "{0}{1}{2}".format(dt.now().year, dt.now().month, dt.now().day)
            desc_parts.append(datestamp)

            desc = sep.join(str(x) for x in desc_parts)
            desc = "".join(str(x) for x in (sep, desc)).strip()

        if opts.feature == "upstream":
            upstream_start, upstream_stop = get_upstream_coords(upstream_site, upstream_len, \
                     seqlen[feat.seqid], feat, children_list, g)

            if not upstream_start or not upstream_stop:
                continue

            feat_seq = f.sequence(dict(chr=feat.seqid, start=upstream_start,
                stop=upstream_stop, strand=feat.strand))

            (s, e) = (upstream_start, upstream_stop) \
                    if feat.strand == "+" else \
                     (upstream_stop, upstream_start)
            upstream_seq_loc = str(feat.seqid) + ":" + str(s) + "-" + str(e)
            desc = sep.join(str(x) for x in (desc, upstream_seq_loc, \
                    "LENGTH=" + str(upstream_len)))
        else:
            children = []
            for c in g.children(feat.id, 1):

                if c.featuretype not in children_list:
                    continue
                child = f.sequence(dict(chr=c.chrom, start=c.start, stop=c.stop,
                    strand=c.strand))
                children.append((child, c))

            if not children:
                print >>sys.stderr, "[warning] %s has no children with type %s" \
                                        % (feat.id, ','.join(children_list))
                continue
            # sort children in incremental position
            children.sort(key=lambda x: x[1].start)
            # reverse children if negative strand
            if feat.strand == '-':
                children.reverse()
            feat_seq = ''.join(x[0] for x in children)

        desc = desc.replace("\"", "")

        id = ",".join(feat.attributes[id_attr]) if id_attr \
                and feat.attributes[id_attr] else \
                feat.id

        rec = SeqRecord(Seq(feat_seq), id=id, description=desc)
        SeqIO.write([rec], fw, "fasta")
        fw.flush()


def parse_feature_param(feature):
    """
    Take the --feature param (coming from gff.load() and parse it.
    Returns feature, parents and children terms.

    Also returns length of upstream sequence (and start site) requested

    If erroneous, returns a flag and error message to be displayed on exit
    """
    import re

    # can request upstream sequence only from the following valid sites
    valid_upstream_sites = ["TSS", "TrSS"]

    upstream_site, upstream_len = None, None
    flag, error_msg = None, None
    if re.match(r'upstream', feature):
        parents, children = "mRNA", "CDS"
        feature, upstream_site, upstream_len = re.search(r'([A-z]+):([A-z]+):(\S+)', \
                feature).groups()

        if not is_number(upstream_len):
            flag, error_msg = 1, "Error: upstream len `" + upstream_len + "` should be an integer"

        upstream_len = int(upstream_len)
        if(upstream_len < 0):
            flag, error_msg = 1, "Error: upstream len `" + str(upstream_len) + "` should be > 0"

        if not upstream_site in valid_upstream_sites:
            flag, error_msg = 1, "Error: upstream site `" + upstream_site + "` not valid." + \
                    " Please choose from " + valid_upstream_site
    elif feature == "CDS":
        parents, children = "mRNA", "CDS"
    else:
        flag, error_msg = 1, "Error: unrecognized option --feature=" + feature

    return feature, parents, children, upstream_site, upstream_len, flag, error_msg


def get_upstream_coords(uSite, uLen, seqlen, feat, children_list, gffdb):
    """
    Subroutine takes upstream site, length, reference sequence length,
    parent mRNA feature (GffLine object), list of child feature types
    and a GFFutils.GFFDB object as the input

    If upstream of TSS is requested, use the parent feature coords
    to extract the upstream sequence

    If upstream of TrSS is requested,  iterates through all the
    children (CDS features stored in the sqlite GFFDB) and use child
    feature coords to extract the upstream sequence

    If success, returns the upstream start and stop coordinates
    else, returns None
    """
    from jcvi.utils.range import range_minmax

    if uSite == "TSS":
        (upstream_start, upstream_stop) = \
                (feat.start - uLen, feat.start - 1) \
                if feat.strand == "+" else \
                (feat.end + 1, feat.end + uLen)
    elif uSite == "TrSS":
        children = []
        for c in gffdb.children(feat.id, 1):

            if c.featuretype not in children_list:
                continue
            children.append((c.start, c.stop))

        if not children:
            print >>sys.stderr, "[warning] %s has no children with type %s" \
                                    % (feat.id, ','.join(children_list))
            return None, None

        cds_start, cds_stop = range_minmax(children)
        (upstream_start, upstream_stop) = \
                (cds_start - uLen, cds_start - 1) \
                if feat.strand == "+" else \
                (cds_stop + 1, cds_stop + uLen)

    if feat.strand == "+" and upstream_start < 1:
        upstream_start = 1
    elif feat.strand == "-" and upstream_stop > seqlen:
        upstream_stop = seqlen

    actual_uLen = upstream_stop - upstream_start + 1
    if actual_uLen < uLen:
        print >>sys.stderr, "[warning] sequence upstream of {0} ({1} bp) is less than upstream length {2}" \
                .format(feat.id, actual_uLen, uLen)
        return None, None

    return upstream_start, upstream_stop


def bed12(args):
    """
    %prog bed12 gffile > bedfile

    Produce bed12 file for coding features. The exons will be converted to blocks.
    The CDS range will be shown between thickStart to thickEnd. For reference,
    bed format consists of the following fields:

    1. chrom
    2. chromStart
    3. chromEnd
    4. name
    5. score
    6. strand
    7. thickStart
    8. thickEnd
    9. itemRgb
    10. blockCount
    11. blockSizes
    12. blockStarts
    """
    p = OptionParser(bed12.__doc__)
    p.add_option("--parent", default="mRNA",
            help="Top feature type [default: %default]")
    p.add_option("--block", default="exon",
            help="Feature type for regular blocks [default: %default]")
    p.add_option("--thick", default="CDS",
            help="Feature type for thick blocks [default: %default]")
    p.set_outfile()
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    gffile, = args
    parent, block, thick = opts.parent, opts.block, opts.thick
    outfile = opts.outfile

    g = make_index(gffile)
    fw = must_open(outfile, "w")

    for f in g.features_of_type(parent):

        chrom = f.chrom
        chromStart = f.start - 1
        chromEnd = f.stop
        name = f.id
        score = 0
        strand = f.strand
        thickStart = 1e15
        thickEnd = 0
        blocks = []

        for c in g.children(name, 1):

            cstart, cend = c.start - 1, c.stop

            if c.featuretype == block:
                blockStart = cstart - chromStart
                blockSize = cend - cstart
                blocks.append((blockStart, blockSize))

            elif c.featuretype == thick:
                thickStart = min(thickStart, cstart)
                thickEnd = max(thickEnd, cend)

        blocks.sort()
        blockStarts, blockSizes = zip(*blocks)
        blockCount = len(blocks)
        blockSizes = ",".join(str(x) for x in blockSizes) + ","
        blockStarts = ",".join(str(x) for x in blockStarts) + ","
        itemRgb = 0

        print >> fw, "\t".join(str(x) for x in (chrom, chromStart, chromEnd, \
                name, score, strand, thickStart, thickEnd, itemRgb,
                blockCount, blockSizes, blockStarts))


if __name__ == '__main__':
    main()
