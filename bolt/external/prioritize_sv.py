"""
Prioritizing SV annotations from SnpEff. Forked from https://github.com/AstraZeneca-NGS/simple_sv_annotation by
    David Jenkins (david.jenkins1@astrazeneca.com/dfj@bu.edu),
    Miika Ahdesmaki (miika.ahdesmaki @ astrazeneca.com / live.fi)

Input:   vcf file with SVs annotated with snpEff 4.3 or higher

Output:  vcf file with tier info in INFO/SV_TOP_TIER field and simplified annotation in INFO/SIMPLE_ANN field

Usage:   simple_sv_annotation input.vcf > output.vcf

Current scheme with priority 1(high)-2(moderate)-3(low)-4(no interest)
- exon loss
   - on prioritisation gene list (1)
   - other (2)
- gene_fusion
   - paired (hits two genes)
      - on list of known pairs (1)
      - one gene is a known promiscuous fusion gene (1)
      - on list of FusionCatcher known pairs (2)
      - other:
         - one or two genes on prioritisation gene list (2)
         - neither gene on prioritisation gene list (3)
   - unpaired (hits one gene)
       - on prioritisation gene list (2)
       - others (3)
- upstream or downstream
   - on prioritisation gene list genes (2)  - e.g. one gene is got into control of another gene's promoter and get overexpressed (oncogene) or underexpressed (tsgene)
- LOF or HIGH impact in a TS gene
   - on prioritisation gene list (2)
   - other TS (3)
- other (4)

Populates:
 - INFO/SIMPLE_ANN that looks like: SIMPLE_ANN=INV|GENE_FUSION|ALK&EML4|NM_...&NM_...|KNOWN_FUSION|1|MODERATE
 - INFO/SV_HIGHEST_TIER (1..4)

See the provided README.md file for more information
"""
import collections
import itertools
import os
import re
import sys


import cyvcf2


def run(
    sv_vcf,
    known_fusion_pairs,
    known_fusion_five,
    known_fusion_three,
    key_genes,
    key_tsgenes,
    bed_annotations_appris,
    output_fp,
):
    """
    Prioritizing structural variants in a VCF file annotated with SnpEff.
    """

    vcf = cyvcf2.VCF(sv_vcf)

    add_cyvcf2_hdr(vcf, 'SIMPLE_ANN', '.', 'String',
        "Simplified structural variant annotation: 'SVTYPE | EFFECT | GENE(s) | TRANSCRIPT | PRIORITY (1-4)'")
    add_cyvcf2_hdr(vcf, 'SV_TOP_TIER', '1', 'Integer',
        "Highest priority tier for the effects of a variant entry")

    w = cyvcf2.Writer(output_fp, vcf)
    w.write_header()

    # TODO: ? Rerun SnpEFF as well to target canonical transcripts, so we don't miss
    #  intergenic variants touching non-canonical transripts?
    princ_tr_by_gid = canon_transcript_per_gene(
        bed_annotations_appris,
        use_gene_id=True,
        only_principal=True,
    )
    all_trs_by_gid = canon_transcript_per_gene(
        bed_annotations_appris,
        use_gene_id=True,
        only_principal=False,
    )
    princ_trs = set(princ_tr_by_gid.values())
    all_trs = set(flatten(all_trs_by_gid.values()))

    # Read in reference data
    known_pairs, fus_promisc = _read_hmf_lists(
        known_fusion_pairs,
        known_fusion_five,
        known_fusion_three,
    )
    prio_genes = _read_list(key_genes)
    tsgenes = _read_list(key_tsgenes)

    # Read in gene lists
    for rec in vcf:
        rec = process_record(
            rec,
            princ_trs,
            all_trs,
            known_pairs,
            fus_promisc,
            prio_genes,
            tsgenes,
        )
        w.write_record(rec)


def _read_list(fpath):
    out_set = set()
    if fpath:
        with open(fpath, 'r') as f:
            for l in f:
                genes = l.strip().split(",")
                if len(genes) == 1 and len(genes[0].strip()) > 0:
                    out_set.add(genes[0].strip())
                if len(genes) == 2 and len(genes[0].strip()) > 0 and len(genes[1].strip()) > 0:
                    out_set.add((genes[0].strip(), genes[1].strip()))
    return out_set


def _read_hmf_lists(pairs_fp, five_fp, three_fp):
    # TODO: check how HMF prioritizes; check if we known order of promiscuous

    known_pairs = set()
    fus_promisc = set()
    with open(pairs_fp) as f:
        for l in f:
            l = l.strip().replace('"', '')
            if l:
                g1, g2 = l.split(',')[0:2]
                if g1 and g2 and g1 != 'H_gene':
                    known_pairs.add((g1, g2))
    with open(five_fp) as f1, open(three_fp) as f2:
        for l in itertools.chain(f1, f2):
            l = l.strip().replace('"', '')
            if l:
                gene = l.split(',')[0]
                if gene and gene != 'gene':
                    fus_promisc.add(gene)

    return known_pairs, fus_promisc


def _check_interaction(all_princ_transcripts, ann, featureid):
    # Splitting 'interaction' feature ids like the following:
    # "3UVN:C_107-D_1494:ENST00000358625-ENST00000262519"
    # -> {'3UVN', 'C_107', 'D_1494', 'ENST00000262519', 'ENST00000358625'}
    feature_ids = set(fid for fid in flatten(fids.split('-') for fids in featureid.split(':')) if fid.startswith('ENST'))
    feature_ids = set([fid.split('.')[0] for fid in feature_ids])  # get rid of version suffix
    assert all(re.fullmatch(r'ENST[\d.]+', fid) for fid in feature_ids), ann
    if not feature_ids - all_princ_transcripts:
        return True
    else:
        return False

    # princ_anns = []
    # alt_anns = []
    # for ann_line in anns:
    #     ann_fields = ann_line.split('|')
    #     assert len(ann_fields) >= 11, f'rec: {rec}, ann_line: {ann_line}'
    #     _, effect, impact, genename, _, feature, featureid, _, rank, _, _ = ann_fields[:11]
    #     # allele, effect, impact, genename, geneid, feature, featureid, biotype, rank, c_change, p_change = ann_fields[:11]
    #
    #     if feature == 'chromosome':
    #         alt_anns.append(ann_line)
    #
    #     elif feature == 'transcript':
    #         feature_ids = set(featureid.split('&'))
    #         feature_ids = set([fid.split('.')[0] for fid in feature_ids])  # get rid of version suffix
    #         assert all(re.fullmatch(r'ENST[\d.]+', fid) for fid in feature_ids), ann
    #         if not feature_ids - princ_trs:
    #             princ_anns.append(ann_line)
    #         elif not feature_ids - all_trs:
    #             alt_anns.append(ann_line)
    #
    #     elif feature == 'interaction':
    #         pass  # skipping altogether
    #         # if _check_interaction(all_princ_transcripts, ann, featureid):
    #         #     new_anns.append(ann_line)
    #
    #     else: # some other events?
    #         print(f'Unrecognizable event {feature}: {ann}, for variant {rec.CHROM}:{rec.POS}', file=sys.stderr)
    #
    # new_anns = princ_anns or alt_anns


def process_record(
    rec,
    princ_trs,
    all_trs,
    known_pairs,
    fus_promisc,
    prio_genes,
    tsgenes,
):

    svtype = rec.INFO.get('SVTYPE', '')
    annos = []
    if rec.INFO.get('ANN') is not None:
        annos = rec.INFO.get('ANN', [])
        if isinstance(annos, str):
            annos = annos.split(',')

    parsed_annos = []
    for anno_string in annos:
        anno_fields = anno_string.split('|')
        if len(anno_fields) < 11:
            continue
        # T|splice_acceptor_variant&splice_region_variant&intron_variant|HIGH|NCOA1|ENSG00000084676|transcript|ENST00000348332.13|
        #   protein_coding|4/20|c.257-52_257-2delTGGAAATAAGCTCTTTTCAGATATGTGATTTTTTTAAGTTTCTTTATTATA||||||INFO_REALIGN_3_PRIME,
        svtype, effect, impact, gene, _, featuretype, featureid, _, rank, _, _ = anno_fields[:11]

        if featuretype == 'interaction':
            continue

        if effect == 'sequence_feature':
            continue

        svtype = svtype.replace('<', '').replace('>', '')
        effects = set(effect.split('&'))
        genes = set([g for g in gene.split('&') if g])  # can be 2 for fusions
        transcript_ids = set([f.split('.')[0] for f in featureid.split('&') if f])
        transcr_id = ''
        if transcript_ids:
            assert len(transcript_ids) == 1
            transcr_id = list(transcript_ids)[0]
            if transcr_id.startswith('ENST'):
                if transcr_id not in all_trs:
                    continue
            elif featuretype == 'chromosome':  # chromosome?
                if not genes:
                    genes = transcript_ids
                transcr_id = ''

        parsed_annos.append((featuretype, effects, impact, genes, transcr_id, rank, anno_string))

    transcripts_by_event = dict()

    for anno in parsed_annos:
        featuretype, effects, impact, genes, transcriptid, rank, anno_string = anno
        ann_tier = 4
        ann_detail = 'unprioritized'

        if effects & {"exon_loss_variant"}:
            assert len(genes) == 1, anno
            gene = list(genes)[0]
            if gene in prio_genes:
                ann_tier = 2
                ann_detail = "key_gene"
            else:
                ann_tier = 3
                ann_detail = "unprioritized"

        elif effects & {"gene_fusion"}:
            # This could be 'gene_fusion', but not 'bidirectional_gene_fusion' or 'feature_fusion'
            # ('gene_fusion' could lead to a coding fusion whereas 'bidirectional_gene_fusion' is
            # likely non-coding (opposing frames, _if_ inference correct))

            # Default tier is 2 (if hitting a prio gene) or 4
            if genes & prio_genes:
                ann_tier = 2
                ann_detail = "key_gene"
            else:
                ann_tier = 4
                ann_detail = "unknown"

            # If exactly 2 genes, checking with the lists of known fusions:
            if len(genes) == 2:
                g1, g2 = genes
                if {(g1, g2), (g2, g1)} & known_pairs:
                    ann_tier = 1
                    ann_detail = "known_pair"

                elif {g1, g2} & fus_promisc:
                    ann_tier = 1
                    ann_detail = "known_promiscuous"

        # "downstream_gene_variant" and "upstream_gene_variant" can also turn out to be a fusion
        # when gene A falls into control of a promoter of gene B
        elif effects & {"downstream_gene_variant", "upstream_gene_variant"}:
            if len(genes) == 2:
                g1, g2 = genes
                if {(g1, g2), (g2, g1)} & known_pairs:
                    ann_tier = 2
                    ann_detail = "known_pair"

                elif {g1, g2} & fus_promisc:
                    ann_tier = 2
                    ann_detail = "known_promiscuous"

            # One of the genes is of interest
            elif genes & prio_genes:
                ann_tier = 3
                ann_detail = "near_key_gene"

        elif impact == 'HIGH' and (genes & tsgenes or featuretype == 'chromosome'):
            if featuretype == 'chromosome':
                ann_tier = 2
                ann_detail = 'chrom_' + '_'.join(genes)

            if genes & tsgenes:
                if genes & prio_genes:
                    ann_tier = 2
                    ann_detail = 'key_tsgene'
                else:
                    ann_tier = 3
                    ann_detail = 'tsgene'

            is_0_cn = is_zero_cn(rec)
            if is_0_cn is False:
                ann_tier += 1
            if is_0_cn is True:
                ann_detail += '_cn0'
                ann_tier -= 1

        else:
            if genes & prio_genes:
                ann_tier = 3
                ann_detail = "key_gene"
                genes = genes & prio_genes
            else:
                ann_tier = 4
                ann_detail = 'unprioritized'

            # gene = ''
            # if genes&prio_genes:
            #     if len(genes&prio_genes) > 2:
            #         gene = f'{len(genes&prio_genes)}_key_genes'
            #     else:
            #         gene = '&'.join(genes&prio_genes)
            # if genes-prio_genes:
            #     if gene:
            #         gene += '&'
            #     if len(genes-prio_genes) > 2:
            #         gene += f'{len(genes-prio_genes)}_other_genes'
            #     else:
            #         gene += '&'.join(genes-prio_genes)

        if not genes:
            pass

        key = (svtype, tuple(effects), tuple(genes), ann_detail, ann_tier)
        # assert len(featureids) == 1
        if key not in transcripts_by_event:
            transcripts_by_event[key] = set()
        if transcriptid:
            if rank:
                transcriptid += '_exon_' + rank
            transcripts_by_event[key].add(transcriptid)
        # assert transcripts_by_event[key]

    top_tier = 4
    simple_annos = set()
    if transcripts_by_event:
        # top_tier = min([k[-1] for k in list(transcripts_by_event.keys())])
        # subset to top events
        # transcripts_by_event = {k: v for k, v in transcripts_by_event.items() if k[-1] == top_tier}

        #TODO: move tanscript principal check here. report all transcripts (princ+alt),
        # but if there are princ exist, and the impact on alt is different, report only princ
        # if len(transcripts_by_event) > 1:  # different events
        # EDIT: perhaps no need in this since we collecting events and comma-separating them

        for event, transcripts in transcripts_by_event.items():
            svtype, effects, genes, ann_detail, ann_tier = event
            simple_anno = (svtype, '&'.join(effects), '&'.join(genes), '&'.join(transcripts), ann_detail, ann_tier)
            simple_annos.add(simple_anno)
            top_tier = min(ann_tier, top_tier)
        # simple_annos = set([(svtype, effect, gene, '&'.join(transcripts), detail, tier)
        #                    for (svtype, effect, gene, detail, tier), transcripts in transcripts_by_event.items()])

    # if len(exon_losses_by_tid) > 0:
    #     losses = annotate_exon_loss(exon_losses_by_tid, prio_genes)
    #     for (gene, transcriptid, deleted_exons, ann_tier) in losses:
    #         simple_annos.add((svtype, 'exon_loss', gene, transcriptid, deleted_exons, ann_tier))
    #         sv_top_tier = min(ann_tier, sv_top_tier)

    # Annotate from LOF
    lofs = rec.INFO.get('LOF', [])  # (GRK7|ENSG00000114124|1|1.00),(DRD3|ENSG00000151577|4|1.00),...
    lof_genes = {l.strip('(').strip(')').split('|')[0] for l in lofs}
    if lof_genes & tsgenes:
        lof_genes = lof_genes & tsgenes
        if lof_genes & prio_genes:
            ann_tier = 2
            ann_detail = 'key_tsgene'
            lof_genes = lof_genes & prio_genes
        else:
            ann_tier = 3
            ann_detail = 'tsgene'

        if is_zero_cn(rec) is False:
            ann_tier += 1
        if is_zero_cn(rec) is True:
            ann_detail += '_cn0'
            ann_tier -= 1

        simple_annos.add((svtype, 'LOF', '&'.join(lof_genes), '', ann_detail, ann_tier))
        top_tier = min(ann_tier, top_tier)

    if not simple_annos:
        simple_annos = [(svtype, 'no_prio_effect', '', '', 'unprioritized', 4)]

    if not annos:
        simple_annos = [(svtype, 'no_func_effect', '', '', 'unprioritized', 4)]

    rec.INFO['SIMPLE_ANN'] = ','.join(['|'.join(map(str, a)) for a in sorted(simple_annos)])
    rec.INFO['SV_TOP_TIER'] = top_tier

    return rec


def is_zero_cn(rec):
    cns = rec.INFO.get('PURPLE_CN')
    if cns is not None:
        if isinstance(cns, float) or isinstance(cns, int):
            cns = [cns]
        return any(float(cn) <= 0.5 for cn in cns)  # real deletion
    return None


def find_deleted_exons(annotations):
    """
    Take the annotations for a particular transcript
    and parse them to find the numbers of the exons that have
    been deleted
    """
    exons = []
    gene = ''
    for anno_fields in annotations:
        _, _, _, g, _, _, _, _, rank, _, _ = anno_fields[:11]
        gene = gene or g
        try:
            exons.append(int(rank.split('/')[0]))
        except ValueError:
            pass
    return exons, gene


def annotate_exon_loss(exon_loss_anno_by_tid, prioritised_genes):
    """
    Create the exon loss simple annotation from the exon dict created in simplify_ann
    For each transcript with exon losses, find the numbers for each exon and create the annotation
    Example: DEL|exon_loss|BLM|NM_001287247.1|exon2-12del
    """

    annos = set()
    for transcript, annotations in exon_loss_anno_by_tid.items():
        exons, gene = find_deleted_exons(annotations)
        exons = list(set(exons))
        if len(exons) == 0:
            return None
        if max(exons) - min(exons) + 1 == len(exons):
            if len(exons) == 1:
                deleted_exons = f'exon{exons[0]}del'
            else:
                deleted_exons = f'exon{min(exons)}-{max(exons)}del'
        else:
            deleted_exons = f'exon{min(exons)}-{max(exons)}del'
        var_priority = 2 if gene in prioritised_genes else 3
        annos.add((gene, transcript, deleted_exons, var_priority))
    return annos




# NOTE(SW): below functions from ngs_util
def add_cyvcf2_hdr(vcf, id, number, type, descr, new_header=None, hdr='INFO'):
    if new_header:
        new_header.append(f'##{hdr}=<ID={id},Number={number},Type={type},Description="{descr}">')
    if hdr == 'INFO':
        vcf.add_info_to_header({'ID': id, 'Type': type, 'Number': number, 'Description': descr})
    elif hdr == 'FORMAT':
        vcf.add_format_to_header({'ID': id, 'Type': type, 'Number': number, 'Description': descr})
    else:
        print(f'Unknown VCF header: {hdr}. Supported: INFO, FORMAT', file=sys.stderr)
        sys.exit(1)


def flatten(l):
    """
    Flatten an irregular list of lists
    example: flatten([[[1, 2, 3], [4, 5]], 6]) -> [1, 2, 3, 4, 5, 6]
    lifted from: http://stackoverflow.com/questions/2158395/
    """
    for el in l:
        if isinstance(el, collections.abc.Iterable) and not isinstance(el, str):
            for sub in flatten(el):
                yield sub
        else:
            yield el


# NOTE(SW): below function from bed_annotation
def canon_transcript_per_gene(appris_fp, only_principal=False, use_gene_id=False):
    """
    Returns a dict of lists: all most confident transcripts per gene according to APPRIS:
    first one in list is PRINCIPAL, the rest are ALTERNATIVE
    If only_principal=True, returns a dict of str, which just one transcript per gene (PRINCIPAL)
    """
    princ_per_gene = dict()
    alt_per_gene = collections.defaultdict(list)
    with open(appris_fp) as f:
        for l in f:
            gene, geneid, enst, ccds, label = l.strip().split('\t')
            if 'PRINCIPAL' in label:
                princ_per_gene[geneid if use_gene_id else gene] = enst
            elif not only_principal and 'ALTERNATIVE' in label:
                alt_per_gene[geneid if use_gene_id else gene].append(enst)

    if only_principal:
        return princ_per_gene
    else:
        return {g: [t] + alt_per_gene[g] for g, t in princ_per_gene.items()}
