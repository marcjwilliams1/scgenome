from scipy.misc import logsumexp as log_sum_exp

import numpy as np
import pandas as pd
import scipy.stats as stats

import dollo.tasks
import dollo.run


def annotate_copy_number(pos, seg, columns=['major', 'minor'], sample_col='sample_id'):
    """ Annotate positions with segment specific data 
    """
    results = []

    for sample_id in seg[sample_col].unique():
        sample_pos = pos[pos[sample_col] == sample_id]
        sample_seg = seg[seg[sample_col] == sample_id]

        for chrom in seg['chr'].unique():
            _pos = sample_pos[sample_pos['chrom'] == chrom]
            _seg = sample_seg[sample_seg['chr'] == chrom]

            results.append(find_overlapping_segments(_pos, _seg, columns))

    return pd.concat(results)


def find_overlapping_segments(pos, seg, columns):
    """ Find positions that are contained within segments
    """
    seg = seg.sort_values(['start', 'end'])

    if seg.duplicated(['start', 'end']).any():
        raise ValueError('duplicate columns')

    start_idx = np.searchsorted(seg['start'].values, pos['coord'].values) - 1
    end_idx = np.searchsorted(seg['end'].values, pos['coord'].values)

    mask = (start_idx == end_idx)

    results = pos.copy()

    for col in columns:
        results[col] = np.nan
        results.loc[mask, col] = seg[col].iloc[end_idx[mask]].values

    return results


def compute_snv_log_likelihoods(snv_data, allele_cn, clusters):
    """ Compute log likelihoods of presence absence for SNVs
    """
    snv_matrix = snv_data.merge(clusters)

    snv_matrix = (
        snv_matrix.groupby(
            ['chrom', 'coord', 'ref', 'alt', 'cluster_id'],
            as_index=True, observed=True)[['alt_counts', 'ref_counts']]
        .sum().unstack().fillna(0).astype(int).stack().reset_index())
    snv_matrix['total_counts'] = snv_matrix['ref_counts'] + snv_matrix['alt_counts']

    snv_matrix['variant_id'] = snv_matrix.apply(
        lambda row: ':'.join(row[['chrom', 'coord', 'ref', 'alt']].astype(str).values),
        axis=1).astype('category')

    # TODO: this should be moved
    allele_cn['total_cn'] = allele_cn['total_cn'].astype(int)
    allele_cn['minor_cn'] = allele_cn['minor_cn'].astype(int)
    allele_cn['major_cn'] = allele_cn['major_cn'].astype(int)
    allele_cn = allele_cn[[
        'chr', 'start', 'end', 'cluster_id',
        'total_cn', 'minor_cn', 'major_cn',
    ]].drop_duplicates()

    # Merge segment copy number into SNV table
    snv_log_likelihoods = annotate_copy_number(
        snv_matrix, allele_cn,
        columns=['major_cn', 'minor_cn', 'total_cn'],
        sample_col='cluster_id')

    snv_log_likelihoods = compute_log_likelihoods(
        snv_log_likelihoods)

    return snv_log_likelihoods


def compute_log_likelihoods(df, error_rate=1e-3):
    """ Compute the presence absence log likelihood of an SNV
    """
    df['log_likelihood_absent'] = df.apply(calculate_likelihood_absent, axis=1, args=(error_rate,))
    df['log_likelihood_present'] = df.apply(calculate_likelihood_present, axis=1, args=(error_rate,))

    return df


def calculate_likelihood_absent(row, e_s):
    return log_likelihood_absent(
        e_s,
        row['alt_counts'],
        row['alt_counts'] + row['ref_counts'],
    )


def calculate_likelihood_present(row, e_s):
    return log_likelihood_present(
        row['major_cn'],
        row['major_cn'] + row['minor_cn'],
        e_s,
        row['alt_counts'],
        row['ref_counts'] + row['alt_counts'],
    )

def log_binomial_pdf(x, n, p):
    return stats.binom.logpmf(x, n, p)


def log_likelihood_absent(e_s, n_v, n_t):
    return log_binomial_pdf(n_v, n_t, e_s)


def log_likelihood_present(c_m, c_t, e_s, n_v, n_t):
    if c_m == 0:
        return log_likelihood_absent(e_s, n_v, n_t)

    conditional_log_likelihoods = []

    for c_v in np.arange(1., c_m + 1., 1.):
        r = c_v / c_t
        conditional_log_likelihoods.append(log_binomial_pdf(n_v, n_t, r))

    return log_sum_exp(conditional_log_likelihoods)


def compute_dollo_ml_tree(snv_log_likelihoods):
    """ Compute the ML tree under the dollo model of SNV evolution
    """
    trees = dollo.tasks.create_trees(snv_log_likelihoods, sample_col='cluster_id')

    results_table = dollo.tasks.compute_tree_log_likelihoods_mp(
        snv_log_likelihoods, trees,
        sample_col='cluster_id', variant_col='variant_id')

    ml_tree_id = results_table.set_index('tree_id')['log_likelihood'].idxmax()

    tree_annotations = dollo.run.annotate_posteriors(
        snv_log_likelihoods, trees[ml_tree_id],
        sample_col='cluster_id', variant_col='variant_id')

    return trees[ml_tree_id], tree_annotations
