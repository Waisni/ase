import sys
import time
import threading

import numpy as np

import ase.parallel
from ase.build import minimize_rotation_and_translation
from ase.calculators.calculator import Calculator
from ase.calculators.singlepoint import SinglePointCalculator
from ase.io import read
from ase.optimize import MDMin
from ase.geometry import find_mic
from ase.io.trajectory import Trajectory
from ase.utils import deprecated
from ase.utils.forcecurve import fit_images
from ase.optimize.precon import make_precon
from ase.optimize.ode import ode12r
from scipy.interpolate import CubicSpline
from scipy.integrate import cumtrapz

class ChainOfStates:
    """
    Base class for NEB-type algorithms which require a chain of images
    """
    def __init__(self, images):
        self.images = images
        self.nsteps = 0
        self.natoms = len(images[0])
        for img in images:
            if len(img) != self.natoms:
                raise ValueError('Images have different numbers of atoms')
            if (img.pbc != images[0].pbc).any():
                raise ValueError('Images have different boundary conditions')
            if (img.get_atomic_numbers() !=
                images[0].get_atomic_numbers()).any():
                raise ValueError('Images have atoms in different orders')
        self.nimages = len(images)

    def interpolate(self, method='linear', mic=False):
        """Interpolate the positions of the interior images between the
        initial state (image 0) and final state (image -1).

        method: str
            Method by which to interpolate: 'linear' or 'idpp'.
            linear provides a standard straight-line interpolation, while
            idpp uses an image-dependent pair potential.
        mic: bool
            Use the minimum-image convention when interpolating.
        """
        interpolate(self.images, mic)

        if method == 'idpp':
            idpp_interpolate(images=self, traj=None, log=None, mic=mic)

    @deprecated("Please use NEB's interpolate(method='idpp') method or "
                "directly call the idpp_interpolate function from ase.neb")
    def idpp_interpolate(self, traj='idpp.traj', log='idpp.log', fmax=0.1,
                         optimizer=MDMin, mic=False, steps=100):
        idpp_interpolate(self, traj=traj, log=log, fmax=fmax,
                         optimizer=optimizer, mic=mic, steps=steps)

    def get_positions(self):
        """Get positions as an array of shape ((nimages-2)*natoms, 3)"""
        positions = np.empty(((self.nimages - 2) * self.natoms, 3))
        n1 = 0
        for image in self.images[1:-1]:
            n2 = n1 + self.natoms
            positions[n1:n2] = image.get_positions()
            n1 = n2
        return positions

    def get_dofs(self):
        """Get degrees of freedom as a long vector"""
        return self.get_positions().reshape(-1)

    def set_positions(self, positions):
        """Set positions from an array of shape ((nimages-2)*natoms, 3)"""
        n1 = 0
        for i, image in enumerate(self.images[1:-1]):
            n2 = n1 + self.natoms
            image.set_positions(positions[n1:n2])
            n1 = n2

    def set_dofs(self, dofs):
        """Set degrees of freedom from a long vector"""
        positions = dofs.reshape(((self.nimages - 2) * self.natoms, 3))
        self.set_positions(positions)

    def __len__(self):
        """Number of degrees of freedom"""
        return (self.nimages - 2) * self.natoms

    def get_forces(self):
        raise NotImplementedError

    def get_fmax_all(self):
        n = self.natoms
        f_i = self.get_forces()
        fmax_images = []
        for i in range(self.nimages - 2):
            n1 = n * i
            n2 = n + n * i
            fmax_images.append(np.sqrt((f_i[n1:n2]**2).sum(axis=1)).max())
        return fmax_images

    def get_potential_energy(self, force_consistent=False):
        raise NotImplementedError

    def iterimages(self):
        # Allows trajectory to convert into several images
        if not self.parallel or self.world.size == 1:
            for atoms in self.images:
                yield atoms
            return

        for i, atoms in enumerate(self.images):
            if i == 0 or i == self.nimages - 1:
                yield atoms
            else:
                atoms = atoms.copy()
                atoms.calc = SinglePointCalculator(energy=self.energies[i],
                                                   forces=self.real_forces[i],
                                                   atoms=atoms)
                yield atoms


class NEB(ChainOfStates):
    def __init__(self, images, k=0.1, fmax=0.05, climb=False, parallel=False,
                 remove_rotation_and_translation=False, world=None,
                 method='aseneb', dynamic_relaxation=False, scale_fmax=0.):
        """Nudged elastic band.

        Paper I:

            G. Henkelman and H. Jonsson, Chem. Phys, 113, 9978 (2000).
            https://doi.org/10.1063/1.1323224

        Paper II:

            G. Henkelman, B. P. Uberuaga, and H. Jonsson, Chem. Phys,
            113, 9901 (2000).
            https://doi.org/10.1063/1.1329672

        Paper III:

            E. L. Kolsbjerg, M. N. Groves, and B. Hammer, J. Chem. Phys,
            145, 094107 (2016)
            https://doi.org/10.1063/1.4961868

        images: list of Atoms objects
            Images defining path from initial to final state.
        k: float or list of floats
            Spring constant(s) in eV/Ang.  One number or one for each spring.
        climb: bool
            Use a climbing image (default is no climbing image).
        parallel: bool
            Distribute images over processors.
        remove_rotation_and_translation: bool
            TRUE actives NEB-TR for removing translation and
            rotation during NEB. By default applied non-periodic
            systems
        dynamic_relaxation: bool
            TRUE calculates the norm of the forces acting on each image
            in the band. An image is optimized only if its norm is above
            the convergence criterion. The list fmax_images is updated
            every force call; if a previously converged image goes out
            of tolerance (due to spring adjustments between the image
            and its neighbors), it will be optimized again. This routine
            can speed up calculations if convergence is non-uniform.
            Convergence criterion should be the same as that given to
            the optimizer. Not efficient when parallelizing over images.
        scale_fmax: float
            Scale convergence criteria along band based on the distance
            between a state and the state with the highest potential energy.
        method: string of method
            Choice between four methods:

            * aseneb: standard ase NEB implementation
            * improvedtangent: Paper I NEB implementation
            * eb: Paper III full spring force implementation
        """
        ChainOfStates.__init__(self, images)
        self.climb = climb
        self.parallel = parallel
        self.emax = np.nan

        self.remove_rotation_and_translation = remove_rotation_and_translation
        self.dynamic_relaxation = dynamic_relaxation
        self.fmax = fmax
        self.scale_fmax = scale_fmax
        if not self.dynamic_relaxation and self.scale_fmax:
            msg = ('Scaled convergence criteria only implemented in series '
                   'with dynamic_relaxation.')
            raise ValueError(msg)

        if method in ['aseneb', 'eb', 'improvedtangent']:
            self.method = method
        else:
            raise NotImplementedError(method)

        if isinstance(k, (float, int)):
            k = [k] * (self.nimages - 1)
        self.k = list(k)

        if world is None:
            world = ase.parallel.world
        self.world = world

        if parallel:
            assert world.size == 1 or world.size % (self.nimages - 2) == 0

        self.real_forces = None  # ndarray of shape (nimages, natom, 3)
        self.energies = None  # ndarray of shape (nimages,)

    def interpolate(self, method='linear', mic=False):
        if self.remove_rotation_and_translation:
            minimize_rotation_and_translation(self.images[0], self.images[-1])
        ChainOfStates.interpolate(self, method, mic)

    def set_positions(self, positions):
        n1 = 0
        for i, image in enumerate(self.images[1:-1]):
            if self.dynamic_relaxation:
                if self.parallel:
                    msg = ('Dynamic relaxation does not work efficiently '
                           'when parallelizing over images. Try AutoNEB '
                           'routine for freezing images in parallel.')
                    raise ValueError(msg)
                else:
                    forces_dyn = self.get_fmax_all(self.images)
                    if forces_dyn[i] < self.fmax:
                        n1 += self.natoms
                    else:
                        n2 = n1 + self.natoms
                        image.set_positions(positions[n1:n2])
                        n1 = n2
            else:
                n2 = n1 + self.natoms
                image.set_positions(positions[n1:n2])
                n1 = n2

    def get_forces(self):
        """Evaluate and return the forces."""
        images = self.images

        calculators = [image.calc for image in images
                       if image.calc is not None]
        if len(set(calculators)) != len(calculators):
            msg = ('One or more NEB images share the same calculator.  '
                   'Each image must have its own calculator.  '
                   'You may wish to use the ase.neb.SingleCalculatorNEB '
                   'class instead, although using separate calculators '
                   'is recommended.')
            raise ValueError(msg)

        forces = np.empty((self.nimages - 2 , self.natoms, 3),  dtype=np.float)
        energies = np.empty(self.nimages)
        x = np.empty((self.nimages - 2, self.natoms, 3), dtype=np.float)

        if self.remove_rotation_and_translation:
            for i in range(1, self.nimages):
                minimize_rotation_and_translation(images[i - 1], images[i])

        if self.method != 'aseneb':
            energies[0] = images[0].get_potential_energy()
            energies[-1] = images[-1].get_potential_energy()
        if not self.parallel:
            # Do all images - one at a time:
            for i in range(1, self.nimages - 1):
                energies[i] = images[i].get_potential_energy()
                forces[i-1] = images[i].get_forces()
                x[i-1] = images[i].get_positions()

        elif self.world.size == 1:
            def run(image, energies, forces):
                energies[:] = image.get_potential_energy()
                forces[:] = image.get_forces()
            threads = [threading.Thread(target=run,
                                        args=(images[i],
                                              energies[i:i + 1],
                                              forces[i - 1:i]))
                       for i in range(1, self.nimages - 1)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        else:
            # Parallelize over images:
            i = self.world.rank * (self.nimages - 2) // self.world.size + 1
            try:
                energies[i] = images[i].get_potential_energy()
                forces[i - 1] = images[i].get_forces()
            except Exception:
                # Make sure other images also fail:
                error = self.world.sum(1.0)
                raise
            else:
                error = self.world.sum(0.0)
                if error:
                    raise RuntimeError('Parallel NEB failed!')

            for i in range(1, self.nimages - 1):
                root = (i - 1) * self.world.size // (self.nimages - 2)
                self.world.broadcast(energies[i:i + 1], root)
                self.world.broadcast(forces[i - 1], root)

        # Save for later use in iterimages:
        self.energies = energies
        self.real_forces = np.zeros((self.nimages, self.natoms, 3))
        self.real_forces[1:-1] = forces

        self.imax = 1 + np.argsort(energies[1:-1])[-1]
        self.emax = energies[self.imax]

        t1 = find_mic(images[1].get_positions() -
                      images[0].get_positions(),
                      images[0].get_cell(), images[0].pbc)[0]

        if self.method == 'eb':
            beeline = (images[self.nimages - 1].get_positions() -
                       images[0].get_positions())
            beelinelength = np.linalg.norm(beeline)
            eqlength = beelinelength / (self.nimages - 1)

        nt1 = np.linalg.norm(t1)


        for i in range(1, self.nimages - 1):
            t2 = find_mic(images[i + 1].get_positions() -
                          images[i].get_positions(),
                          images[i].get_cell(), images[i].pbc)[0]
            nt2 = np.linalg.norm(t2)

            if self.method == 'eb':
                # Tangents are bisections of spring-directions
                # (formula C8 of paper III)
                tangent = t1 / nt1 + t2 / nt2
                # Normalize the tangent vector
                tangent /= np.linalg.norm(tangent)
            elif self.method == 'improvedtangent':
                # Tangents are improved according to formulas 8, 9, 10,
                # and 11 of paper I.
                if energies[i + 1] > energies[i] > energies[i - 1]:
                    tangent = t2.copy()
                elif energies[i + 1] < energies[i] < energies[i - 1]:
                    tangent = t1.copy()
                else:
                    deltavmax = max(abs(energies[i + 1] - energies[i]),
                                    abs(energies[i - 1] - energies[i]))
                    deltavmin = min(abs(energies[i + 1] - energies[i]),
                                    abs(energies[i - 1] - energies[i]))
                    if energies[i + 1] > energies[i - 1]:
                        tangent = t2 * deltavmax + t1 * deltavmin
                    else:
                        tangent = t2 * deltavmin + t1 * deltavmax
                # Normalize the tangent vector
                tangent /= np.linalg.norm(tangent)
            else:
                if i < self.imax:
                    tangent = t2
                elif i > self.imax:
                    tangent = t1
                else:
                    tangent = t1 + t2
                tt = np.vdot(tangent, tangent)

            f = forces[i - 1]
            ft = np.vdot(f, tangent)

            if i == self.imax and self.climb:
                # imax not affected by the spring forces. The full force
                # with component along the elestic band converted
                # (formula 5 of Paper II)
                if self.method == 'aseneb':
                    f -= 2 * ft / tt * tangent
                else:
                    f -= 2 * ft * tangent
            elif self.method == 'eb':
                f -= ft * tangent
                # Spring forces
                # (formula C1, C5, C6 and C7 of Paper III)
                f1 = -(nt1 - eqlength) * t1 / nt1 * self.k[i - 1]
                f2 = (nt2 - eqlength) * t2 / nt2 * self.k[i]
                if self.climb and abs(i - self.imax) == 1:
                    deltavmax = max(abs(energies[i + 1] - energies[i]),
                                    abs(energies[i - 1] - energies[i]))
                    deltavmin = min(abs(energies[i + 1] - energies[i]),
                                    abs(energies[i - 1] - energies[i]))
                    f += (f1 + f2) * deltavmin / deltavmax
                else:
                    f += f1 + f2
            elif self.method == 'improvedtangent':
                f -= ft * tangent
                # Improved parallel spring force (formula 12 of paper I)
                f_spring = (nt2 * self.k[i] - nt1 * self.k[i - 1]) * tangent
                f += f_spring

            else:
                f -= ft / tt * tangent
                f -= np.vdot(t1 * self.k[i - 1] -
                             t2 * self.k[i], tangent) / tt * tangent

            t1 = t2
            nt1 = nt2

            if self.dynamic_relaxation:
                n = self.natoms
                k = i - 1
                n1 = n * k
                n2 = n1 + n
                force_i = np.sqrt((forces.reshape((-1, 3))[n1:n2]**2.)
                                  .sum(axis=1)).max()

                n1_imax = (self.imax - 1) * n
                positions = self.get_positions()
                pos_imax = positions[n1_imax:n1_imax + n]
                rel_pos = np.sqrt(((positions[n1:n2] - pos_imax)**2).sum())

                if force_i < self.fmax * (1 + rel_pos * self.scale_fmax):
                    if k == self.imax - 1:
                        pass
                    else:
                        forces[k, :, :] = np.zeros((1, self.natoms, 3))

        return forces.reshape((-1, 3))

    def get_potential_energy(self, force_consistent=False):
        """Return the maximum potential energy along the band.
        Note that the force_consistent keyword is ignored and is only
        present for compatibility with ase.Atoms.get_potential_energy."""
        return self.emax



class IDPP(Calculator):
    """Image dependent pair potential.

    See:
        Improved initial guess for minimum energy path calculations.
        Søren Smidstrup, Andreas Pedersen, Kurt Stokbro and Hannes Jónsson
        Chem. Phys. 140, 214106 (2014)
    """

    implemented_properties = ['energy', 'forces']

    def __init__(self, target, mic):
        Calculator.__init__(self)
        self.target = target
        self.mic = mic

    def calculate(self, atoms, properties, system_changes):
        Calculator.calculate(self, atoms, properties, system_changes)

        P = atoms.get_positions()
        d = []
        D = []
        for p in P:
            Di = P - p
            if self.mic:
                Di, di = find_mic(Di, atoms.get_cell(), atoms.get_pbc())
            else:
                di = np.sqrt((Di**2).sum(1))
            d.append(di)
            D.append(Di)
        d = np.array(d)
        D = np.array(D)

        dd = d - self.target
        d.ravel()[::len(d) + 1] = 1  # avoid dividing by zero
        d4 = d**4
        e = 0.5 * (dd**2 / d4).sum()
        f = -2 * ((dd * (1 - 2 * dd / d) / d**5)[..., np.newaxis] * D).sum(0)
        self.results = {'energy': e, 'forces': f}


class SingleCalculatorNEB(NEB):
    def __init__(self, images, k=0.1, climb=False, index=None):
        if isinstance(images, str):
            # this is a filename
            images = read(images, index=index)

        NEB.__init__(self, images, k, climb, False)
        self.calculators = [None] * self.nimages
        self.energies_ok = False
        self.first = True

    def interpolate(self, initial=0, final=-1, mic=False):
        """Interpolate linearly between initial and final images."""
        if final < 0:
            final = self.nimages + final
        n = final - initial
        pos1 = self.images[initial].get_positions()
        pos2 = self.images[final].get_positions()
        dist = (pos2 - pos1)
        if mic:
            cell = self.images[initial].get_cell()
            assert((cell == self.images[final].get_cell()).all())
            pbc = self.images[initial].get_pbc()
            assert((pbc == self.images[final].get_pbc()).all())
            dist, D_len = find_mic(dist, cell, pbc)
        dist /= n
        for i in range(1, n):
            self.images[initial + i].set_positions(pos1 + i * dist)

    def refine(self, steps=1, begin=0, end=-1, mic=False):
        """Refine the NEB trajectory."""
        if end < 0:
            end = self.nimages + end
        j = begin
        n = end - begin
        for i in range(n):
            for k in range(steps):
                self.images.insert(j + 1, self.images[j].copy())
                self.calculators.insert(j + 1, None)
            self.k[j:j + 1] = [self.k[j] * (steps + 1)] * (steps + 1)
            self.nimages = len(self.images)
            self.interpolate(j, j + steps + 1, mic=mic)
            j += steps + 1

    def set_positions(self, positions):
        # new positions -> new forces
        if self.energies_ok:
            # restore calculators
            self.set_calculators(self.calculators[1:-1])
        NEB.set_positions(self, positions)

    def get_calculators(self):
        """Return the original calculators."""
        calculators = []
        for i, image in enumerate(self.images):
            if self.calculators[i] is None:
                calculators.append(image.calc)
            else:
                calculators.append(self.calculators[i])
        return calculators

    def set_calculators(self, calculators):
        """Set new calculators to the images."""
        self.energies_ok = False
        self.first = True

        if not isinstance(calculators, list):
            calculators = [calculators] * self.nimages

        n = len(calculators)
        if n == self.nimages:
            for i in range(self.nimages):
                self.images[i].calc = calculators[i]
        elif n == self.nimages - 2:
            for i in range(1, self.nimages - 1):
                self.images[i].calc = calculators[i - 1]
        else:
            raise RuntimeError(
                'len(calculators)=%d does not fit to len(images)=%d'
                % (n, self.nimages))

    def get_energies_and_forces(self):
        """Evaluate energies and forces and hide the calculators"""
        if self.energies_ok:
            return

        self.emax = -1.e32

        def calculate_and_hide(i):
            image = self.images[i]
            calc = image.calc
            if self.calculators[i] is None:
                self.calculators[i] = calc
            if calc is not None:
                if not isinstance(calc, SinglePointCalculator):
                    self.images[i].calc = SinglePointCalculator(
                        image,
                        energy=image.get_potential_energy(
                            apply_constraint=False),
                        forces=image.get_forces(apply_constraint=False))
                self.emax = min(self.emax, image.get_potential_energy())

        if self.first:
            calculate_and_hide(0)

        # Do all images - one at a time:
        for i in range(1, self.nimages - 1):
            calculate_and_hide(i)

        if self.first:
            calculate_and_hide(-1)
            self.first = False

        self.energies_ok = True

    def get_forces(self):
        self.get_energies_and_forces()
        return NEB.get_forces(self)

    def n(self):
        return self.nimages

    def write(self, filename):
        traj = Trajectory(filename, 'w', self)
        traj.write()
        traj.close()

    def __add__(self, other):
        for image in other:
            self.images.append(image)
        return self


def interpolate(images, mic=False):
    """Given a list of images, linearly interpolate the positions of the
    interior images."""
    pos1 = images[0].get_positions()
    pos2 = images[-1].get_positions()
    d = pos2 - pos1
    if mic:
        d = find_mic(d, images[0].get_cell(), images[0].pbc)[0]
    d /= (len(images) - 1.0)
    for i in range(1, len(images) - 1):
        images[i].set_positions(pos1 + i * d)


def idpp_interpolate(images, traj='idpp.traj', log='idpp.log', fmax=0.1,
                     optimizer=MDMin, mic=False, steps=100):
    """Interpolate using the IDPP method. 'images' can either be a plain
    list of images or an NEB object (containing a list of images)."""
    if hasattr(images, 'interpolate'):
        neb = images
    else:
        neb = NEB(images)
    d1 = neb.images[0].get_all_distances(mic=mic)
    d2 = neb.images[-1].get_all_distances(mic=mic)
    d = (d2 - d1) / (neb.nimages - 1)
    real_calcs = []
    for i, image in enumerate(neb.images):
        real_calcs.append(image.calc)
        image.calc = IDPP(d1 + i * d, mic=mic)
    opt = optimizer(neb, trajectory=traj, logfile=log)
    opt.run(fmax=fmax, steps=steps)
    for image, calc in zip(neb.images, real_calcs):
        image.calc = calc


class NEBTools:
    """Class to make many of the common tools for NEB analysis available to
    the user. Useful for scripting the output of many jobs. Initialize with
    list of images which make up one or more band of the NEB relaxation."""

    def __init__(self, images):
        self.images = images

    @deprecated('NEBTools.get_fit() is deprecated.  '
                'Please use ase.utils.forcecurve.fit_images(images).')
    def get_fit(self):
        return fit_images(self.images)

    def get_barrier(self, fit=True, raw=False):
        """Returns the barrier estimate from the NEB, along with the
        Delta E of the elementary reaction. If fit=True, the barrier is
        estimated based on the interpolated fit to the images; if
        fit=False, the barrier is taken as the maximum-energy image
        without interpolation. Set raw=True to get the raw energy of the
        transition state instead of the forward barrier."""
        forcefit = fit_images(self.images)
        energies = forcefit.energies
        fit_energies = forcefit.fit_energies
        dE = energies[-1] - energies[0]
        if fit:
            barrier = max(fit_energies)
        else:
            barrier = max(energies)
        if raw:
            barrier += self.images[0].get_potential_energy()
        return barrier, dE

    def get_fmax(self, **kwargs):
        """Returns fmax, as used by optimizers with NEB."""
        neb = NEB(self.images, **kwargs)
        forces = neb.get_forces()
        return np.sqrt((forces**2).sum(axis=1).max())

    def plot_band(self, ax=None):
        """Plots the NEB band on matplotlib axes object 'ax'. If ax=None
        returns a new figure object."""
        forcefit = fit_images(self.images)
        ax = forcefit.plot(ax=ax)
        return ax.figure

    def plot_bands(self, constant_x=False, constant_y=False,
                   nimages=None, label='nebplots'):
        """Given a trajectory containing many steps of a NEB, makes
        plots of each band in the series in a single PDF.

        constant_x: bool
            Use the same x limits on all plots.
        constant_y: bool
            Use the same y limits on all plots.
        nimages: int
            Number of images per band. Guessed if not supplied.
        label: str
            Name for the output file. .pdf will be appended.
        """
        from matplotlib import pyplot
        from matplotlib.backends.backend_pdf import PdfPages
        if nimages is None:
            nimages = self._guess_nimages()
        nebsteps = len(self.images) // nimages
        if constant_x or constant_y:
            sys.stdout.write('Scaling axes.\n')
            sys.stdout.flush()
            # Plot all to one plot, then pull its x and y range.
            fig, ax = pyplot.subplots()
            for index in range(nebsteps):
                images = self.images[index * nimages:(index + 1) * nimages]
                NEBTools(images).plot_band(ax=ax)
                xlim = ax.get_xlim()
                ylim = ax.get_ylim()
            pyplot.close(fig)  # Reference counting "bug" in pyplot.
        with PdfPages(label + '.pdf') as pdf:
            for index in range(nebsteps):
                sys.stdout.write('\rProcessing band {:10d} / {:10d}'
                                 .format(index, nebsteps))
                sys.stdout.flush()
                fig, ax = pyplot.subplots()
                images = self.images[index * nimages:(index + 1) * nimages]
                NEBTools(images).plot_band(ax=ax)
                if constant_x:
                    ax.set_xlim(xlim)
                if constant_y:
                    ax.set_ylim(ylim)
                pdf.savefig(fig)
                pyplot.close(fig)  # Reference counting "bug" in pyplot.
        sys.stdout.write('\n')

    def _guess_nimages(self):
        """Attempts to guess the number of images per band from
        a trajectory, based solely on the repetition of the
        potential energy of images. This should also work for symmetric
        cases."""
        e_first = self.images[0].get_potential_energy()
        nimages = None
        for index, image in enumerate(self.images[1:], start=1):
            e = image.get_potential_energy()
            if e == e_first:
                # Need to check for symmetric case when e_first = e_last.
                try:
                    e_next = self.images[index + 1].get_potential_energy()
                except IndexError:
                    pass
                else:
                    if e_next == e_first:
                        nimages = index + 1  # Symmetric
                        break
                nimages = index  # Normal
                break
        if nimages is None:
            sys.stdout.write('Appears to be only one band in the images.\n')
            return len(self.images)
        # Sanity check that the energies of the last images line up too.
        e_last = self.images[nimages - 1].get_potential_energy()
        e_nextlast = self.images[2 * nimages - 1].get_potential_energy()
        if not (e_last == e_nextlast):
            raise RuntimeError('Could not guess number of images per band.')
        sys.stdout.write('Number of images per band guessed to be {:d}.\n'
                         .format(nimages))
        return nimages


class NEBtools(NEBTools):
    @deprecated('NEBtools has been renamed; please use NEBTools.')
    def __init__(self, images):
        NEBTools.__init__(self, images)


@deprecated('Please use NEBTools.plot_band_from_fit.')
def plot_band_from_fit(s, E, Sfit, Efit, lines, ax=None):
    NEBTools.plot_band_from_fit(s, E, Sfit, Efit, lines, ax=None)


def fit0(*args, **kwargs):
    raise DeprecationWarning('fit0 is deprecated. Use `fit_raw` from '
                             '`ase.utils.forcecurve` instead.')


class PreconMEP(ChainOfStates):
    def __init__(self, images, precon='Exp', method='string', k=0.1,
                 adapt_spring_constants=None, logfile='-', get_all_forces=None,
                 get_all_potential_energies=None):
        """
        Preconditioned minimum energy path finding.

        This class implements preconditoned variants of the NEB and String
        methods, as described in the following article:

                S. Makri, C. Ortner and J. R. Kermode, J. Chem. Phys.
                150, 094109 (2019)
                https://dx.doi.org/10.1063/1.5064465

        """
        ChainOfStates.__init__(self, images)

        # build initial preconditioner and make a copy for each image
        self.precon_method = precon
        P0 = make_precon(precon)
        P0.make_precon(self.images[0])
        self.precon = [P0]
        for i in range(1, self.nimages):
            P = P0.copy()
            P.make_precon(self.images[i])
            self.precon.append(P)

        method = method.lower()
        methods = ['neb', 'string']
        if method not in methods:
            raise ValueError(f'method must be one of {methods}')
        self.method = method

        if isinstance(logfile, str):
            if logfile == "-":
                logfile = sys.stdout
            else:
                logfile = open(logfile, "a")
        self.logfile = logfile

        if isinstance(k, (float, int)):
            k = [k] * (self.nimages - 1)
        self.k = list(k)
        self.adapt_spring_constants = adapt_spring_constants

        self.residuals = np.empty(self.nimages - 2)
        self.fmax_history = []

        self.get_all_forces = get_all_forces
        self.get_all_potential_energies = get_all_potential_energies

    def spline_fit(self, norm='precon'):
        """
        Fit cubic splines to image positions (and optionally forces)

        Returns
        -------
            s, x_spline[, f_spline]
        """

        d_P = np.zeros(self.nimages)
        x = np.zeros((self.nimages, 3 * self.natoms))  # flattened positions
        x[0, :] = self.images[0].positions.reshape(-1)

        for i in range(1, self.nimages):
            x[i, :] = self.images[i].positions.reshape(-1)
            dx, _ = find_mic(self.images[i].positions -
                             self.images[i - 1].positions,
                             self.images[i - 1].cell,
                             self.images[i - 1].pbc)
            dx = dx.reshape(-1)

            # distance defined in Eq. 8 in the paper
            if norm == 'precon':
                d_P[i] = np.sqrt(0.5*(self.precon[i].dot(dx, dx) +
                                      self.precon[i - 1].dot(dx, dx)))
            else:
                d_P[i] = norm(dx)

        s = d_P.cumsum() / d_P.sum()  # Eq. A1 in the paper
        x_spline = CubicSpline(s, x, bc_type='not-a-knot')
        return s, x_spline

    def get_forces(self):
        """Evaluate and return the forces."""
        images = self.images

        forces = np.empty(((self.nimages - 2), self.natoms, 3), dtype=np.float)
        if self.get_all_forces is not None:
            forces[...] = self.get_all_forces(images[1:-1])
        else:
            calculators = [image.calc for image in images
                           if image.calc is not None]
            if len(set(calculators)) != len(calculators):
                msg = ('One or more NEB images share the same calculator.  '
                       'Each image must have its own calculator.  ')
                raise ValueError(msg)
            for i in range(1, self.nimages - 1):
                forces[i - 1] = self.images[i].get_forces()

        s, x_spline = self.spline_fit()
        dx_ds = x_spline.derivative()
        d2x_ds2 = x_spline.derivative(2)

        self.residuals[:] = 0

        # Evaluate forces for all images - one at a time
        for i in range(1, self.nimages - 1):
            f = forces[i - 1]
            f_vec = f.reshape(-1)

            # update preconditioners for each image and apply to forces
            # this implements part of Eq. 6: pf_vec = - P^{-1} * \nabla V(x)
            pf_vec, _ = self.precon[i].apply(f_vec, images[i])

            # Project out the component parallel to band following Eqs. 6 and 7
            t_P = dx_ds(s[i])
            t_P /= self.precon[i].norm(t_P)
            pf_vec -= np.dot(t_P, f_vec) * t_P

            # Definition of residuals on each image from Eq. 11
            self.residuals[i - 1] = np.linalg.norm(self.precon[i].Pdot(pf_vec),
                                                   np.inf)

            # print(f'norm(pf_{i}) = {np.linalg.norm(pf_vec, np.inf)}')

            if self.method == 'neb':
                # Definition following Eq. 9
                k = 0.5 * (self.k[i - 1]  + self.k[i]) / (self.nimages ** 2)
                eta_Pn = k * self.precon[i].dot(d2x_ds2(s[i]), t_P) * t_P

                # print(f'norm(eta_{i}) = {np.linalg.norm(eta_Pn, np.inf)}')

                # complete Eq. 9 by including the spring force
                pf_vec += eta_Pn

            forces[i - 1] = pf_vec.reshape((self.natoms, 3))

        return forces # FIXME shape not consistent with NEB.get_forces()

    def get_potential_energies(self):
        if self.get_all_potential_energies is not None:
            energies = self.get_all_potential_energies(self.images)
        else:
            energies = [image.get_potential_energy() for image in self.images]
        return np.array(energies)

    def get_fmax_all(self):
        return self.residuals[:]

    def get_residual(self, F=None, X=None):
        return np.max(self.residuals) # Eq. 11

    def integrate_forces(self, spline_points=1000, bc_type='not-a-knot',
                         return_forces=False):
        """
        Use spline fit to integrate forces along MEP to approximate
        energy differences using the virtual work approach.

        Parameters
        ----------
        spline_points - number of spline points to use
        return_forces - if True, include forces in results as well as energies

        Returns
        -------

        s - reaction coordinate in range [0, 1], with `spline_points` entries
        E - result of integrating forces, on the same grid as `s`.
        F - if return_forces is True, also return projected forces along MEP
        """
        # note we use standard Euclidean rather than preconditioned norm
        # to compute the virtual work
        s, x = self.spline_fit(norm=np.linalg.norm)
        forces = np.array([image.get_forces().reshape(-1)
                           for image in self.images])
        f = CubicSpline(s, forces, bc_type=bc_type)

        dx = x.derivative()
        s = np.linspace(0.0, 1.0, spline_points, endpoint=True)
        dE = f(s) * dx(s)
        F = dE.sum(axis=1)
        E = -cumtrapz(F, s, initial=0.0)
        if return_forces:
            return s, E, F
        else:
            return s, E

    def log(self):
        fmax = self.get_residual()
        self.fmax_history.append(fmax)
        T = time.localtime()
        if self.logfile is not None:
            name = (f'{self.__class__.__name__}[{self.method},'
                    f'{self.step_selection},{self.precon_method}]')
            if self.nsteps == 0:
                args = (
                " " * len(name), "Step", "Time", "fmax")
                msg = "%s  %4s %8s %12s\n" % args
                self.logfile.write(msg)

            args = (name, self.nsteps, T[3], T[4], T[5], fmax)
            msg = "%s:  %3d %02d:%02d:%02d %12.4f\n" % args
            self.logfile.write(msg)
            self.logfile.flush()

    def callback(self, X):
        self.log()
        self.nsteps += 1

        if self.method == 'string':
            # for string we need to reparameterise after each update step
            self.set_dofs(X)
            s, x_spline = self.spline_fit()
            new_s = np.linspace(0, 1, self.nimages)
            X[:] = x_spline(new_s[1:-1]).reshape(-1)
        elif self.method == 'neb' and self.adapt_spring_constants:
            self.k[:] = self.adapt_spring_constants(self.k, self.images)

    def force_function(self, X):
        self.set_dofs(X)
        f = self.get_forces()
        return f.reshape(-1)

    def run(self, fmax=1e-3, steps=50, step_selection='ODE', alpha=0.01,
            verbose=0, rtol=0.1, C1=1e-2, C2=2.0):
        """
        Optimize images to obtain the minimum energy path

        Parameters
        ----------
        fmax - desired force tolerance
        steps - maximum number of steps
        step_selection - either 'ODE' or 'static
        alpha - step length if step_selection = 'static'.
        verbose, rtol, C1, C2 - passed along to ODE12r if step_selection = 'static'

        """
        step_selection = step_selection.lower()
        step_selections = ['ode', 'static']
        if step_selection not in step_selections:
            raise ValueError(f'optimizer must be one of {step_selections}')
        self.step_selection = step_selection # save for logging purposes

        if step_selection == 'ode':
            ode12r(self.force_function,
                   self.get_dofs(),
                   fmax=fmax,
                   rtol=rtol,
                   C1=C1,
                   C2=C2,
                   steps=steps,
                   verbose=verbose,
                   callback=self.callback,
                   residual=self.get_residual)
        else:
            X = self.get_dofs()
            for step in range(steps):
                F = self.force_function(X)
                if self.get_residual() <= fmax:
                    break
                X += alpha * F
                self.callback(X)
