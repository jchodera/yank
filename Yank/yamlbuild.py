#!/usr/bin/env python

#=============================================================================================
# MODULE DOCSTRING
#=============================================================================================

"""
Tools to build Yank experiments from a YAML configuration file.

"""

#=============================================================================================
# GLOBAL IMPORTS
#=============================================================================================

import os
import re
import copy
import yaml
import logging
logger = logging.getLogger(__name__)

import numpy as np
import openmoltools
from simtk import unit, openmm

import utils
import pipeline
from yank import Yank
from repex import ReplicaExchange, ThermodynamicState
from sampling import ModifiedHamiltonianExchange


#=============================================================================================
# UTILITY FUNCTIONS
#=============================================================================================

def compute_min_dist(mol_positions, *args):
    for pos1 in args:
        # Compute squared distances
        # Each row is an array of distances from a mol2 atom to all mol1 atoms
        distances2 = np.array([((pos1 - pos2)**2).sum(1) for pos2 in mol_positions])

        # Find closest atoms and their distance
        min_idx = np.unravel_index(distances2.argmin(), distances2.shape)
        try:
            min_dist = min(min_dist, np.sqrt(distances2[min_idx]))
        except UnboundLocalError:
            min_dist = np.sqrt(distances2[min_idx])
    return min_dist

def remove_overlap(mol_positions, *args, **kwargs):
    x = np.copy(mol_positions)
    sigma = kwargs.get('sigma', 1.0)
    min_distance = kwargs.get('min_distance', 1.0)

    # Try until we have a non-overlapping conformation w.r.t. all fixed molecules
    while compute_min_dist(x, *args) <= min_distance:
        # Compute center of geometry
        x0 = x.mean(0)

        # Randomize orientation of ligand.
        q = ModifiedHamiltonianExchange._generate_uniform_quaternion()
        Rq = ModifiedHamiltonianExchange._rotation_matrix_from_quaternion(q)
        x = ((Rq * np.matrix(x - x0).T).T + x0).A

        # Choose a random displacement vector and translate
        x += sigma * np.random.randn(3)

    return x

def to_openmm_app(str):
    return getattr(openmm.app, str)

#=============================================================================================
# BUILDER CLASS
#=============================================================================================

class YamlParseError(Exception):
    """Represent errors occurring during parsing of Yank YAML file."""
    def __init__(self, message):
        super(YamlParseError, self).__init__(message)
        logger.error(message)

class YamlBuilder:
    """Parse YAML configuration file and build the experiment.

    Properties
    ----------
    options : dict
        The options specified in the parsed YAML file.

    """

    SETUP_DIR = 'setup'
    SETUP_SYSTEMS_DIR = os.path.join(SETUP_DIR, 'systems')
    SETUP_MOLECULES_DIR = os.path.join(SETUP_DIR, 'molecules')
    EXPERIMENTS_DIR = 'experiments'

    DEFAULT_OPTIONS = {
        'verbose': False,
        'mpi': False,
        'resume_setup': False,
        'resume_simulation': False,
        'output_dir': 'output/',
        'temperature': 298 * unit.kelvin,
        'pressure': 1 * unit.atmosphere,
        'constraints': openmm.app.HBonds,
        'hydrogenMass': 1 * unit.amu
    }

    @property
    def yank_options(self):
        return self._isolate_yank_options(self.options)

    def __init__(self, yaml_source):
        """Parse the given YAML configuration file.

        This does not build the actual experiment but simply checks that the syntax
        is correct and loads the configuration into memory.

        Parameters
        ----------
        yaml_source : str
            A path to the YAML script or the YAML content.

        """

        self._oe_molecules = {}  # molecules generated by OpenEye
        self._fixed_pos_cache = {}  # positions of molecules given as files

        # TODO check version of yank-yaml language
        # TODO what if there are multiple streams in the YAML file?
        try:
            with open(yaml_source, 'r') as f:
                yaml_content = yaml.load(f)
        except IOError:
            yaml_content = yaml.load(yaml_source)

        if yaml_content is None:
            raise YamlParseError('The YAML file is empty!')

        # Save raw YAML content that will be needed when generating the YAML files
        self._raw_yaml = copy.deepcopy({key: yaml_content.get(key, {})
                                        for key in ['options', 'molecules', 'solvents']})

        # Parse each section
        self._parse_options(yaml_content)
        self._parse_molecules(yaml_content)
        self._parse_solvents(yaml_content)
        self._parse_experiments(yaml_content)

    def build_experiment(self):
        """Build the Yank experiment."""
        self._check_setup_resume()

        for output_dir, combination in self._expand_experiments():
            self._run_experiment(combination, output_dir)

    @classmethod
    def _get_molecule_setup_dir(cls, output_dir, molecule_id):
        return os.path.join(output_dir, cls.SETUP_MOLECULES_DIR, molecule_id)

    @classmethod
    def _get_system_setup_dir(cls, output_dir, receptor_id, ligand_id, solvent_id):
        system_dir = '_'.join((receptor_id, ligand_id, solvent_id))
        return os.path.join(output_dir, cls.SETUP_SYSTEMS_DIR, system_dir)

    @classmethod
    def _get_experiment_dir(cls, output_dir, experiment_dir):
        return os.path.join(output_dir, cls.EXPERIMENTS_DIR, experiment_dir)

    def _validate_options(self, options):
        """Return a dictionary with YamlBuilder and Yank options validated."""
        template_options = self.DEFAULT_OPTIONS.copy()
        template_options.update(Yank.default_parameters)
        template_options.update(ReplicaExchange.default_parameters)
        openmm_app_type = {'constraints': to_openmm_app}
        try:
            valid = utils.validate_parameters(options, template_options, check_unknown=True,
                                              process_units_str=True, float_to_int=True,
                                              special_conversions=openmm_app_type)
        except (TypeError, ValueError) as e:
            raise YamlParseError(str(e))
        return valid

    def _isolate_yank_options(self, options):
        return {opt: val for opt, val in options.items()
                if opt not in self.DEFAULT_OPTIONS}

    def _parse_options(self, yaml_content):
        # Merge options and metadata and validate
        temp_options = yaml_content.get('options', {})
        temp_options.update(yaml_content.get('metadata', {}))

        # Validate options and fill in default values
        self.options = self.DEFAULT_OPTIONS.copy()
        self.options.update(self._validate_options(temp_options))

    def _parse_molecules(self, yaml_content):
        file_formats = set(['mol2', 'pdb'])
        sources = set(['filepath', 'name', 'smiles'])
        template_mol = {'filepath': 'str', 'name': 'str', 'smiles': 'str',
                        'parameters': 'str', 'epik': 0}

        self._molecules = yaml_content.get('molecules', {})

        # First validate and convert
        for molecule_id, molecule in self._molecules.items():
            try:
                self._molecules[molecule_id] = utils.validate_parameters(molecule, template_mol,
                                                                         check_unknown=True)
            except (TypeError, ValueError) as e:
                raise YamlParseError(str(e))

        err_msg = ''
        for molecule_id, molecule in self._molecules.items():
            fields = set(molecule.keys())

            # Check that only one source is specified
            specified_sources = sources & fields
            if not specified_sources or len(specified_sources) > 1:
                err_msg = ('need only one between {} for molecule {}').format(
                    ', '.join(list(sources)), molecule_id)

            # Check supported file formats
            elif 'filepath' in specified_sources:
                extension = os.path.splitext(molecule['filepath'])[1][1:]  # remove '.'
                if extension not in file_formats:
                    err_msg = 'molecule {}, only {} files supported'.format(
                        molecule_id, ', '.join(file_formats))

            # Check that parameters are specified
            if 'parameters' not in fields:
                err_msg = 'no parameters specified for molecule {}'.format(molecule_id)

            if err_msg != '':
                raise YamlParseError(err_msg)

    def _parse_solvents(self, yaml_content):
        template_parameters = {'nonbondedMethod': openmm.app.PME, 'nonbondedCutoff': 1 * unit.angstroms,
                               'implicitSolvent': openmm.app.OBC2, 'clearance': 10.0 * unit.angstroms}
        openmm_app_type = ('nonbondedMethod', 'implicitSolvent')
        openmm_app_type = {option: to_openmm_app for option in openmm_app_type}

        self._solvents = yaml_content.get('solvents', {})

        # First validate and convert
        for solvent_id, solvent in self._solvents.items():
            try:
                self._solvents[solvent_id] = utils.validate_parameters(solvent, template_parameters,
                                                         check_unknown=True, process_units_str=True,
                                                         special_conversions=openmm_app_type)
            except (TypeError, ValueError, AttributeError) as e:
                raise YamlParseError(str(e))

        err_msg = ''
        for solvent_id, solvent in self._solvents.items():

            # Test mandatory parameters
            if 'nonbondedMethod' not in solvent:
                err_msg = 'solvent {} must specify nonbondedMethod'.format(solvent_id)
                raise YamlParseError(err_msg)

            # Test solvent consistency
            nonbonded_method = solvent['nonbondedMethod']
            if nonbonded_method == openmm.app.NoCutoff:
                if 'nonbondedCutoff' in solvent:
                    err_msg = ('solvent {} specify both nonbondedMethod: NoCutoff and '
                               'and nonbondedCutoff').format(solvent_id)
            else:
                if 'implicitSolvent' in solvent:
                    err_msg = ('solvent {} specify both nonbondedMethod: {} '
                               'and implicitSolvent').format(solvent_id, nonbonded_method)
                elif 'clearance' not in solvent:
                    err_msg = ('solvent {} uses explicit solvent but '
                               'no clearance specified').format(solvent_id)

            # Raise error
            if err_msg != '':
                raise YamlParseError(err_msg)

    def _expand_experiments(self):
        """Generator to generated experiments with output directory."""
        output_dir = ''
        for exp_name, experiment in self._experiments.items():
            if len(self._experiments) > 1:
                output_dir = exp_name

            # Loop over all combinations
            for name, combination in experiment.named_combinations(separator='_', max_name_length=40):
                yield os.path.join(output_dir, name), combination

    def _parse_experiments(self, yaml_content):
        """Perform dry run and validate components and options of every combination."""
        experiment_template = {'components': {}, 'options': {}}
        components_template = {'receptor': 'str', 'ligand': 'str', 'solvent': 'str'}

        # Check if there is a sequence of experiments or a single one
        try:
            self._experiments = {exp_name: utils.CombinatorialTree(yaml_content[exp_name])
                                 for exp_name in yaml_content['experiments']}
        except KeyError:
            self._experiments = yaml_content.get('experiment', {})
            if self._experiments:
                self._experiments = {'experiment': utils.CombinatorialTree(self._experiments)}

        # Check validity of every experiment combination
        err_msg = ''
        for exp_name, exp in self._expand_experiments():
            if exp_name == '':
                exp_name = 'experiment'

            # Check if we can identify components
            if 'components' not in exp:
                raise YamlParseError('Cannot find components for {}'.format(exp_name))
            components = exp['components']

            # Validate and check for unknowns
            try:
                utils.validate_parameters(exp, experiment_template, check_unknown=True)
                utils.validate_parameters(components, components_template, check_unknown=True)
                self._validate_options(exp.get('options', {}))
            except (ValueError, TypeError) as e:
                raise YamlParseError(str(e))

            # Check that components have been specified
            if components['receptor'] not in self._molecules:
                err_msg = 'Cannot identify receptor for {}'.format(exp_name)
            elif components['ligand'] not in self._molecules:
                err_msg = 'Cannot identify ligand for {}'.format(exp_name)
            elif components['solvent'] not in self._solvents:
                err_msg = 'Cannot identify solvent for {}'.format(exp_name)

            if err_msg != '':
                raise YamlParseError(err_msg)

    def _check_setup_resume(self):
        """Perform dry run to check if we are going to overwrite setup files."""
        err_msg = ''
        for exp_sub_dir, combination in self._expand_experiments():
            try:
                output_dir = combination['options']['output_dir']
            except KeyError:
                output_dir = self.options['output_dir']
            try:
                resume_setup = combination['options']['resume_setup']
            except KeyError:
                resume_setup = self.options['resume_setup']
            try:
                resume_sim = combination['options']['resume_simulation']
            except KeyError:
                resume_sim = self.options['resume_simulation']

            # Identify components
            components = combination['components']
            receptor_id = components['receptor']
            ligand_id = components['ligand']
            solvent_id = components['solvent']

            # Check molecule setup dirs
            for molecule_id in [receptor_id, ligand_id]:
                molecule_dir = self._get_molecule_setup_dir(output_dir, molecule_id)
                if os.path.exists(molecule_dir) and not resume_setup:
                    err_msg = 'molecule setup directory {}'.format(molecule_dir)
                    break

            # Check system setup dirs
            system_dir = self._get_system_setup_dir(output_dir, receptor_id, ligand_id, solvent_id)
            if os.path.exists(system_dir) and not resume_setup:
                err_msg = 'system setup directory {}'.format(system_dir)

            if err_msg != '':
                solving_option = 'resume_setup'

            # Check experiment dir
            experiment_dir = self._get_experiment_dir(output_dir, exp_sub_dir)
            if os.path.exists(experiment_dir) and not resume_sim:
                err_msg = 'experiment directory {}'.format(experiment_dir)
                solving_option = 'resume_simulation'

            # Check for errors
            if err_msg != '':
                err_msg += (' already exists; cowardly refusing to proceed. Move/delete '
                            'directory or set {} options').format(solving_option)
                raise YamlParseError(err_msg)

    def _generate_molecule(self, molecule_id):
        """Generate molecule and save it to mol2 in molecule['filepath']."""
        mol_descr = self._molecules[molecule_id]
        try:
            if 'name' in mol_descr:
                molecule = openmoltools.openeye.iupac_to_oemol(mol_descr['name'])
            elif 'smiles' in mol_descr:
                molecule = openmoltools.openeye.smiles_to_oemol(mol_descr['smiles'])
            molecule = openmoltools.openeye.get_charges(molecule, keep_confs=1)
        except ImportError as e:
            error_msg = ('requested molecule generation from name or smiles but '
                         'could not find OpenEye toolkit: ' + str(e))
            raise YamlParseError(error_msg)

        return molecule

    def _setup_molecules(self, output_dir, *args):
        """OpenEye-generated molecules can change position from one experiment to another
        depeding on positions of other fixed molecules."""

        # Determine which molecules should have fixed positions
        # At the end of parametrization we update the 'filepath' key also for OpenEye-generated
        # molecules so we need to check that the molecule is not in self._oe_molecules as well
        file_mol_ids = {mol_id for mol_id in args if 'filepath' in self._molecules[mol_id] and
                        mol_id not in self._oe_molecules}

        # Generate missing molecules with OpenEye
        self._oe_molecules.update({mol_id: self._generate_molecule(mol_id) for mol_id in args
                                   if mol_id not in file_mol_ids and mol_id not in self._oe_molecules})

        # Check that non-generated molecules don't have overlapping atoms
        # TODO this check should be available even without OpenEye
        # TODO also there should be an option allowing to solve the overlap in this case too?
        fixed_pos = {}  # positions of molecules from files of THIS setup
        if utils.is_openeye_installed():
            # We need positions as a list so we separate the ids and positions in two lists
            mol_id_list = list(file_mol_ids)
            positions = [0 for _ in mol_id_list]
            for i, mol_id in enumerate(mol_id_list):
                try:
                    positions[i] = self._fixed_pos_cache[mol_id]
                except KeyError:
                    positions[i] = utils.get_oe_mol_positions(utils.read_oe_molecule(
                        self._molecules[mol_id]['filepath']))

            # Verify that distances between any pair of fixed molecules is big enough
            for i in range(len(positions) - 1):
                posi = positions[i]
                if compute_min_dist(posi, *positions[i+1:]) < 0.1:
                    raise YamlParseError('The given molecules have overlapping atoms!')

            # Convert positions list to dictionary, this is needed to solve overlaps
            fixed_pos = {mol_id_list[i]: positions[i] for i in range(len(mol_id_list))}

            # Cache positions for future molecule setups
            self._fixed_pos_cache.update(fixed_pos)

        # Find and solve overlapping atoms in OpenEye generated molecules
        for mol_id in args:
            # Retrive OpenEye-generated molecule
            try:
                molecule = self._oe_molecules[mol_id]
            except KeyError:
                continue
            molecule_pos = utils.get_oe_mol_positions(molecule)

            # Remove overlap and save new positions
            if fixed_pos:
                molecule_pos = remove_overlap(molecule_pos, *(fixed_pos.values()),
                                              min_distance=1.0, sigma=1.0)
                utils.set_oe_mol_positions(molecule, molecule_pos)

            # Update fixed positions for next round
            fixed_pos[mol_id] = molecule_pos

        # Save parametrized molecules
        for mol_id in args:
            mol_descr = self._molecules[mol_id]

            # Have we already processed this molecule? Do we have to do it at all?
            # We don't want to create the output folder if we don't need to
            if not (mol_id in self._oe_molecules or mol_descr['parameters'] == 'antechamber'):
                continue

            # Create output directory and handle resume, we always process OpenEye-generated
            # molecules because they may change position in different system to resolve
            # overlapping atoms.
            mol_dir = self._get_molecule_setup_dir(output_dir, mol_id)
            if not os.path.exists(mol_dir):
                os.makedirs(mol_dir)
            elif mol_id not in self._oe_molecules:
                continue

            # Write OpenEye generated molecules in mol2 files
            if mol_id in self._oe_molecules:
                # We update the 'filepath' key in the molecule description
                mol_descr['filepath'] = os.path.join(mol_dir, mol_id + '.mol2')

                # We set the residue name as the first three uppercase letters
                residue_name = re.sub('[^A-Za-z]+', '', mol_id.upper())
                openmoltools.openeye.molecule_to_mol2(molecule, mol_descr['filepath'],
                                                      residue_name=residue_name)

            # Enumerate protonation states with epik
            if 'epik' in mol_descr:
                epik_idx = mol_descr['epik']
                epik_output_file = os.path.join(mol_dir, mol_id + '-epik.mol2')
                utils.run_epik(mol_descr['filepath'], epik_output_file, extract_range=epik_idx)
                mol_descr['filepath'] = epik_output_file

            # Parametrize the molecule with antechamber
            if mol_descr['parameters'] == 'antechamber':
                # Generate parameters
                input_mol_path = os.path.abspath(mol_descr['filepath'])
                with utils.temporary_cd(mol_dir):
                    openmoltools.amber.run_antechamber(mol_id, input_mol_path)

                # Save new parameters paths, this way if we try to
                # setup the molecule again it will just be skipped
                mol_descr['filepath'] = os.path.join(mol_dir, mol_id + '.gaff.mol2')
                mol_descr['parameters'] = os.path.join(mol_dir, mol_id + '.frcmod')

    def _setup_system(self, output_dir, components):

        # Identify components
        receptor_id = components['receptor']
        ligand_id = components['ligand']
        solvent_id = components['solvent']
        receptor = self._molecules[receptor_id]
        ligand = self._molecules[ligand_id]
        solvent = self._solvents[solvent_id]

        # Create output directory and check if system has bee already processed
        system_dir = self._get_system_setup_dir(output_dir, receptor_id, ligand_id, solvent_id)
        if os.path.exists(system_dir):
            return system_dir
        os.makedirs(system_dir)

        # Setup molecules
        self._setup_molecules(output_dir, receptor_id, ligand_id)

        # Create tleap script
        tleap = utils.TLeap()
        tleap.new_section('Load GAFF parameters')
        tleap.load_parameters('leaprc.gaff')

        # Check that AMBER force field is specified
        if not ('leaprc.' in receptor['parameters'] or 'leaprc.' in ligand['parameters']):
            tleap.load_parameters('leaprc.ff14SB')

        # Load receptor and ligand
        for group_name in ['receptor', 'ligand']:
            group = self._molecules[components[group_name]]
            tleap.new_section('Load ' + group_name)
            tleap.load_parameters(group['parameters'])
            tleap.load_group(name=group_name, file_path=group['filepath'])

        # Create complex
        tleap.new_section('Create complex')
        tleap.combine('complex', 'receptor', 'ligand')

        # Configure solvent
        if solvent['nonbondedMethod'] == openmm.app.NoCutoff:
            if 'implicitSolvent' in solvent:  # GBSA implicit solvent
                tleap.new_section('Set GB radii to recommended values for OBC')
                tleap.add_commands('set default PBRadii mbondi2')
        else:  # explicit solvent
            tleap.new_section('Solvate systems')
            clearance = float(solvent['clearance'].value_in_unit(unit.angstroms))
            tleap.solvate(group='complex', water_model='TIP3PBOX', clearance=clearance)
            tleap.solvate(group='ligand', water_model='TIP3PBOX', clearance=clearance)

        # Check charge
        tleap.new_section('Check charge')
        tleap.add_commands('check complex', 'charge complex')

        # Save prmtop and inpcrd files
        tleap.new_section('Save prmtop and inpcrd files')
        tleap.save_group('complex', os.path.join(system_dir, 'complex.prmtop'))
        tleap.save_group('complex', os.path.join(system_dir, 'complex.pdb'))
        tleap.save_group('ligand', os.path.join(system_dir, 'solvent.prmtop'))
        tleap.save_group('ligand', os.path.join(system_dir, 'solvent.pdb'))

        # Save tleap script for reference
        tleap.export_script(os.path.join(system_dir, 'leap.in'))

        # Run tleap!
        tleap.run()

        return system_dir

    def _generate_yaml(self, experiment, output_dir, file_name=''):
        """Generate the minimum YAML file describing the experiment."""
        components = set(experiment['components'].values())

        # Molecules section data
        mol_section = {mol_id: molecule for mol_id, molecule in self._raw_yaml['molecules'].items()
                       if mol_id in components}

        # Solvents section data
        sol_section = {solvent_id: solvent for solvent_id, solvent in self._raw_yaml['solvents'].items()
                       if solvent_id in components}

        # We pop the options section in experiment and merge it to the general one
        exp_section = experiment.copy()
        opt_section = self._raw_yaml['options'].copy()
        opt_section.update(exp_section.pop('options', {}))

        # Create YAML with the sections in order
        yaml_content = yaml.dump({'options': opt_section}, default_flow_style=False, line_break='\n', explicit_start=True)
        yaml_content += yaml.dump({'molecules': mol_section}, default_flow_style=False, line_break='\n')
        yaml_content += yaml.dump({'solvents': sol_section}, default_flow_style=False, line_break='\n')
        yaml_content += yaml.dump({'experiment': exp_section}, default_flow_style=False, line_break='\n')

        # Export YAML into a file
        with open(os.path.join(output_dir, file_name), 'w') as f:
            f.write(yaml_content)

    def _run_experiment(self, experiment, experiment_dir):
        components = experiment['components']
        exp_name = 'experiment' if experiment_dir == '' else os.path.basename(experiment_dir)

        # Get and validate experiment sub-options
        exp_opts = self.options.copy()
        exp_opts.update(self._validate_options(experiment.get('options', {})))
        yank_opts = self._isolate_yank_options(exp_opts)

        # Configure MPI, if requested
        if exp_opts['mpi']:
            from mpi4py import MPI
            MPI.COMM_WORLD.barrier()
            mpicomm = MPI.COMM_WORLD
        else:
            mpicomm = None

        # TODO configure platform and precision when they are fixed in Yank

        # Create directory and configure logger for this experiment
        results_dir = self._get_experiment_dir(exp_opts['output_dir'], experiment_dir)
        if not os.path.isdir(results_dir):
            os.makedirs(results_dir)
            resume = False
        else:
            resume = True
        utils.config_root_logger(exp_opts['verbose'], os.path.join(results_dir, exp_name + '.log'))

        # Initialize simulation
        yank = Yank(results_dir, mpicomm=mpicomm, **yank_opts)

        if resume:
            yank.resume()
        else:
            # Export YAML file for reproducibility
            self._generate_yaml(experiment, results_dir, exp_name + '.yaml')

            # Determine system files path
            system_dir = self._setup_system(exp_opts['output_dir'], components)

            # Get ligand resname for alchemical atom selection
            ligand_dsl = utils.get_mol2_resname(self._molecules[components['ligand']]['filepath'])
            if ligand_dsl is None:
                ligand_dsl = 'MOL'
            ligand_dsl = 'resname ' + ligand_dsl

            # System configuration
            create_system_filter = set(('nonbondedMethod', 'nonbondedCutoff', 'implicitSolvent',
                                        'constraints', 'hydrogenMass'))
            solvent = self._solvents[components['solvent']]
            system_pars = {opt: solvent[opt] for opt in create_system_filter if opt in solvent}
            system_pars.update({opt: exp_opts[opt] for opt in create_system_filter
                                if opt in exp_opts})

            # Prepare system
            phases, systems, positions, atom_indices = pipeline.prepare_amber(system_dir, ligand_dsl, system_pars)

            # Create thermodynamic state
            thermodynamic_state = ThermodynamicState(temperature=exp_opts['temperature'],
                                                     pressure=exp_opts['pressure'])

            # Create new simulation
            yank.create(phases, systems, positions, atom_indices, thermodynamic_state)

        # Run the simulation!
        yank.run()


if __name__ == "__main__":
    import doctest
    doctest.testmod()
