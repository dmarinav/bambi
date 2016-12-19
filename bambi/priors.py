import numpy as np
import pandas as pd
from pandas import Series
from os.path import dirname, join
from bambi.external.six import string_types
from copy import deepcopy
import json
import re
import statsmodels.api as sm


class Family(object):

    '''
    A specification of model family.
    Args:
        name (str): Family name
        prior (Prior): A Prior instance specifying the model likelihood prior
        link (str): The name of the link function transforming the linear
            model prediction to a parameter of the likelihood
        parent (str): The name of the prior parameter to set to the link-
            transformed predicted outcome (e.g., mu, p, etc.).
    '''

    def __init__(self, name, prior, link, parent):
        self.name = name
        self.prior = prior
        self.link = link
        self.parent = parent
        fams = {
            'gaussian': sm.families.Gaussian,
            'binomial': sm.families.Binomial,
            'poisson': sm.families.Poisson,
            't': None # not implemented in statsmodels
        }
        self.smfamily = fams[name] if name in fams.keys() else None


class Prior(object):

    '''
    Abstract specification of a term prior.
    Args:
        name (str): Name of prior distribution (e.g., Normal, Binomial, etc.)
        kwargs (dict): Optional keywords specifying the parameters of the
            named distribution.
    '''

    def __init__(self, name, **kwargs):
        self.name = name
        self.args = {}
        self.update(**kwargs)

    def update(self, **kwargs):
        '''
        Update the model arguments with additional arguments.
        Args:
            kwargs (dict): Optional keyword arguments to add to prior args.
        '''
        self.args.update(kwargs)


class PriorFactory(object):

    '''
    An object that supports specification and easy retrieval of default priors.
    Args:
        defaults (str, dict): Optional base configuration containing default
            priors for distribution, families, and term types. If a string,
            the name of a JSON file containing the config. If a dict, must
            contain keys for 'dists', 'terms', and 'families'; see the built-in
            JSON configuration for an example. If None, a built-in set of
            priors will be used as defaults.
        dists (dict): Optional specification of named distributions to use
            as priors. Each key gives the name of a newly defined distribution;
            values are two-element lists, where the first element is the name
            of the built-in distribution to use ('Normal', 'Cauchy', etc.),
            and the second element is a dictionary of parameters on that
            distribution (e.g., {'mu': 0, 'sd': 10}). Priors can be nested
            to arbitrary depths by replacing any parameter with another prior
            specification.
        terms (dict): Optional specification of default priors for different
            model term types. Valid keys are 'intercept', 'fixed', or 'random'.
            Values are either strings preprended by a #, in which case they
            are interpreted as pointers to distributions named in the dists
            dictionary, or key -> value specifications in the same format as
            elements in the dists dictionary.
        families (dict): Optional specification of default priors for named
            family objects. Keys are family names, and values are dicts
            containing mandatory keys for 'dist', 'link', and 'parent'.

    Examples:
        >>> dists = { 'my_dist': ['Normal', {'mu': 10, 'sd': 1000}]}
        >>> pf = PriorFactory(dists=dists)

        >>> families = { 'normalish': { 'dist': ['normal', {sd: '#my_dist'}],
        >>>                             link:'identity', parent: 'mu'}}
        >>> pf = PriorFactory(dists=dists, families=families)
    '''

    def __init__(self, defaults=None, dists=None, terms=None, families=None):

        if defaults is None:
            defaults = join(dirname(__file__), 'config', 'priors.json')

        if isinstance(defaults, string_types):
            defaults = json.load(open(defaults, 'r'))

        # Just in case the user plans to use the same defaults elsewhere
        defaults = deepcopy(defaults)

        if isinstance(dists, dict):
            defaults['dists'].update(dists)

        if isinstance(terms, dict):
            defaults['terms'].update(terms)

        if isinstance(families, dict):
            defaults['families'].update(families)

        self.dists = defaults['dists']
        self.terms = defaults['terms']
        self.families = defaults['families']

    def _get_prior(self, spec):

        if isinstance(spec, string_types):
            spec = re.sub('^\#', '', spec)
            return self._get_prior(self.dists[spec])
        elif isinstance(spec, (list, tuple)):
            name, args = spec
            if name.startswith('#'):
                name = re.sub('^\#', '', name)
                prior = self._get_prior(self.dists[name])
            else:
                prior = Prior(name)
            args = {k: self._get_prior(v) for (k, v) in args.items()}
            prior.update(**args)
            return prior
        else:
            return spec

    def get(self, dist=None, term=None, family=None, **kwargs):
        '''
        Retrieve default prior for a named distribution, term type, or family.
        Args:
            dist (str): Name of desired distribution. Note that the name is
                the key in the defaults dictionary, not the name of the
                Distribution object used to construct the prior.
            term (str): The type of term family to retrieve defaults for.
                Must be one of 'intercept', 'fixed', or 'random'.
            family (str): The name of the Family to retrieve. Must be a value
                defined internally. In the default config, this is one of
                'gaussian', 'binomial', 'poisson', or 't'.
        '''
        if dist is not None:
            if dist not in self.dists:
                raise ValueError(
                    "'%s' is not a valid distribution name." % dist)
            return self._get_prior(self.dists[dist])
        elif term is not None:
            if term not in self.terms:
                raise ValueError("'%s' is not a valid term type." % term)
            return self._get_prior(self.terms[term])
        elif family is not None:
            if family not in self.families:
                raise ValueError("'%s' is not a valid family name." % family)
            _f = self.families[family]
            prior = self._get_prior(_f['dist'])
            return Family(family, prior, _f['link'], _f['parent'])


class PriorScaler(object):

    # Default is 'wide'. The wide prior SD is sqrt(1/3) = .577 on the partial
    # corr scale, which is the SD of a flat prior over [-1,1].
    names = {
        'narrow': 0.2,
        'medium': 0.4,
        'wide': 3 ** -0.5,
        'superwide': 0.8
    }

    def __init__(self, model):
        self.model = model
        self.stats = model.dm_statistics if hasattr(model, 'dm_statistics') \
            else None
        self.dm = pd.DataFrame({'{}[{}]'.format(t.name, lev): t.data[:, lev]
                   for t in model.fixed_terms.values()
                   for lev in range(len(t.levels))})
        self.priors = {}

    def _scale_fixed(self, term, value):

        # these defaults are only defined for Normal priors
        if term.prior.name != 'Normal':
            return

        mu = []
        sd = []
        for pred in term.data.T:
            # figure out which column of dm to drop for the null model
            keeps = [i for i,x in enumerate(list(self.dm.columns))
                if not np.array_equal(pred, self.dm[x].values.flatten())]

            # fit null model
            null = sm.GLM(endog=self.model.y.data,
                exog=self.dm[keeps] if keeps \
                    else np.repeat(0, len(self.model.y.data)),
                family=self.model.family.smfamily(),
                missing='drop' if self.model.dropna else 'none').fit()

            # compute 2nd derivative of log-likelihood w.r.t. predictor
            mod = sm.GLM(endog=self.model.y.data, exog=self.dm,
                family=self.model.family.smfamily(),
                missing='drop' if self.model.dropna else 'none')
            params = pd.Series([0]*len(mod.exog_names), index=mod.exog_names,
                dtype='float64')
            if hasattr(null.params, 'index'):
                for lab, val in zip(null.params.index, null.params):
                    params[lab] = val
            mod.fit()
            pos = [x for x in range(len(params)) if x not in keeps][0]
            d2 = mod.hessian(params=params, scale=null.scale,
                observed=True)[pos,pos]
            # should check that d2 is < 0 ?

            # get and return tuning parameter
            mu += [0]
            sd += [(len(self.model.y.data) * np.log(1 - value**2) / d2)**.5]

        # save and set prior
        # NOTE: SDs expanded by factor of 2 for now (remove this hack later)
        self.priors.update({term.name: {
            'mu':np.array(mu), 'sd':2*np.array(sd), 'levels':term.levels,
            }})
        term.prior.update(mu = np.array(mu), sd=2*np.array(sd))

    def _scale_intercept(self, term, value):

        # default priors are only defined for Normal priors
        if term.prior.name != 'Normal':
            return

        # start with mean and variance of Y on the link scale
        mod = sm.GLM(endog=self.model.y.data,
            exog=np.repeat(1, len(self.model.y.data)),
            family=self.model.family.smfamily(),
            missing='drop' if self.model.dropna else 'none').fit()
        mu = mod.params
        # multiply SE by sqrt(N) to turn it into (approx.) SD(Y) on link scale
        sd = (mod.cov_params()[0] * len(mod.mu))**.5

        # modify mu and sd based on means and SDs of slope priors
        if len(self.priors):
            # get order
            index = sum([p['levels'] for p in self.priors.values()], [])
            # get slope prior means and SDs
            means = np.concatenate([p['mu'] \
                for p in self.priors.values()]).ravel()
            means = pd.Series(means, index=index)
            sds = np.concatenate([p['sd'] \
                for p in self.priors.values()]).ravel()
            sds = pd.Series(sds, index=index)
            # add to intercept prior
            mu -= np.dot(means, self.stats['mean_x'][index])
            sd = (sd**2 + np.dot(sds**2, self.stats['mean_x'][index]**2))**.5

        # save and set prior
        self.priors.update({term.name: {
            'mu':np.array(mu), 'sd':2*np.array(sd), 'levels':term.levels,
            }})
        term.prior.update(mu=mu, sd=sd)

    def _scale_random(self, term, value):
        # classify as random intercept or random slope
        term_type = 'intercept' if '|' not in term.name else 'slope'

        # these default priors are only defined for HalfNormal priors
        if term.prior.args['sd'].name != 'HalfNormal':
            return

        # handle random slopes
        if term_type == 'slope':
            # get name of corresponding fixed effect
            fix = re.sub(r'\|.*', r'', term.name).strip()

            # handle case of multiple fixed effects, including intercept
            if self.stats is not None:
                # handle case where there is a corresponding fixed term
                if fix in list(self.stats['r2_y'].index):
                    slope_constant = self.stats['sd_y'] * \
                        (1 - self.stats['r2_y'][fix])**.5 / \
                        self.stats['sd_x'][fix] / \
                        (1 - self.stats['r2_x'][fix])**.5
                else:
                    # recreate the corresponding fixed effect data
                    fix_data = term.data.sum(axis=1) \
                        if not isinstance(term.data, dict) \
                        else np.vstack([term.data[x].sum(axis=1) \
                        for x in term.data.keys()]).T
                    # set prior on standardized beta instead of partial corr
                    slope_constant = self.stats['sd_y'] / fix_data.std()

            # handle the case where fixed fx are intercept-only or cell-means
            else:
                fix_data = term.data.sum(axis=1) \
                    if not isinstance(term.data, dict) \
                    else np.vstack([term.data[x].sum(axis=1) \
                    for x in term.data.keys()]).T
                # more than 1 column implies this these are cell-means random fx
                # sum over rows so that resulting mu = mean(Y)
                if len(fix_data.shape) > 1 and fix_data.shape[1] > 1:
                    fix_data = fix_data.sum(axis=1)
                mu = np.dot(fix_data.flatten(), self.model.y.data) / \
                    np.dot(fix_data.flatten(), fix_data.flatten())
                mu_shift = np.dot(fix_data.flatten(),
                    self.model.y.data + self.model.y.data.std()) / \
                    np.dot(fix_data.flatten(), fix_data.flatten())
                slope_constant = np.abs(mu - mu_shift)

            term.prior.args['sd'].update(sd=value * np.asscalar(slope_constant))

        # handle random intercepts
        else:
            # this handles the usual cases: usually, intercept and >0 fixed fx
            # less commonly, cell means + covariate model (i.e., no intercept)
            if self.stats is not None:
                index = list(self.stats['r2_y'].index)
                sd = self.stats['sd_y'] * \
                    (1 - self.stats['r2_y'][index])**.5 / \
                    self.stats['sd_x'][index] / \
                    (1 - self.stats['r2_x'][index])**.5
                sd = np.dot(sd**2, self.stats['mean_x'][index]**2)**.5
            # this handles case where fixed fx are intercept-only or cell-means
            else:
                # use the same sd as we use in a fixed-intercept-only model
                sd = self.model.y.data.std()
            term.prior.args['sd'].update(sd=value * sd)

    def scale(self):
        # classify all terms
        fixed_intercepts = [t for t in self.model.terms.values()
            if not t.random and t.data.sum(1).var()==0]
        fixed_slopes = [t for t in self.model.terms.values()
            if not t.random and not t.data.sum(1).var()==0]
        random_terms = [t for t in self.model.terms.values() if t.random]

        # arrange them in the order in which they should be initialized
        term_list = fixed_slopes + fixed_intercepts + random_terms
        term_types = ['fixed']*len(fixed_slopes) + \
            ['intercept']*len(fixed_intercepts) + \
            ['random']*len(random_terms)

        # initialize them in order
        for t, term_type in zip(term_list, term_types):

            # only set default priors if no prior defined yet
            if not isinstance(t.prior, Prior):

                # decide scale
                value = t.prior
                if value is None:
                    if not self.model.auto_scale:
                        return
                    value = 'wide'

                # set scale
                if isinstance(value, string_types):
                    value = PriorScaler.names[value]

                # impute default
                t.prior = self.model.default_priors.get(term=term_type)

                # scale it!
                getattr(self, '_scale_%s' % term_type)(t, value)

