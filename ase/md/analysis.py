import numpy as np

class DiffusionCoefficient:

    def __init__(self, traj, timestep, steps_between_saved_images, atom_indices=None, molecule=False):
        '''
        Calculates Diffusion Coefficients for atoms and molecules

        Parameters:
            traj (Trajectory): 
                Trajectory of atoms objects (images)
            timestep (Int): 
                Timestep used between each images
            steps_between_saved_images (Int): 
                Interval used when writing the .traj file 
            molecule_index (List of Int): 
                The indices of atoms whose Diffusion Coefficient is to be calculated

            This function calculates the Diffusion Coefficient for the given .traj file using the Einstein Equation:
            ⟨|r(t)−r(0)|**2⟩ = 6Dt (where r(t) is the position of atom at time t, D is the Diffusion Coefficient)
            Solved herein as y = mx + c, i.e. 1/6 ⟨|r(t)−r(0)|**2⟩ = Dt, so m = D, c = 0

            wiki : https://en.wikibooks.org/wiki/Molecular_Simulation/Diffusion_Coefficients
        '''

        self.traj = traj
        self.timestep = timestep
        self.steps_between_saved_images = steps_between_saved_images

        # Condition used if user wants to calculate diffusion coefficients for specific atoms or all atoms
        self.atom_indices = atom_indices
        if self.atom_indices == None:
            self.atom_indices = [i for i in range(len(traj[0]))] 

        # Condition if we are working with the mobility of a molecule, need to manage arrays slightly differently
        self.molecule = molecule
        if molecule:
            self.types_of_atoms = ["molecule"]
            self.no_of_atoms = [1]
        else:
            self.types_of_atoms = sorted(list(set(np.take(traj[0].get_chemical_symbols(), self.atom_indices))))
            self.no_of_atoms = [traj[0].get_chemical_symbols().count(symbol) for symbol in self.types_of_atoms]

        self.no_of_types_of_atoms = len(self.types_of_atoms)

    def __initialise_arrays(self, ignore_n_images, number_of_segments):

        '''
        
        '''

        from math import floor
        total_images = len(self.traj) - ignore_n_images
        self.no_of_segments = number_of_segments
        self.len_segments = floor(total_images/self.no_of_segments)

        time_between_images = self.timestep * self.steps_between_saved_images
        # These are the data objects we need when plotting information. First the x-axis, timesteps
        self.timesteps = np.arange(0,total_images*time_between_images,time_between_images)
        # This holds all the data points for the diffusion coefficients, averaged over atoms
        self.xyz_segment_ensemble_average = np.zeros((self.no_of_segments,self.no_of_types_of_atoms,3,self.len_segments))
        # This holds all the information on linear fits, from which we get the diffusion coefficients
        self.slopes = np.zeros((self.no_of_types_of_atoms,self.no_of_segments,3))
        self.intercepts = np.zeros((self.no_of_types_of_atoms,self.no_of_segments,3))

        self.cont_xyz_segment_ensemble_average = 0

    def calculate(self, ignore_n_images=0, number_of_segments=1):
        '''
        Calculate the diffusion coefficients.

        Parameter:
            ignore_n_images (Int): 
                Number of images you want to ignore from the start of the trajectory, e.g. during equilibration
            number_of_segments (Int): 
                Divides the given trajectory in to segments to allow statistical analysis
        '''

        # Setup all the arrays we need to store information
        self.__initialise_arrays(ignore_n_images, number_of_segments)

        for segment_no in range(self.no_of_segments):
            start = segment_no*self.len_segments  
            end = start + self.len_segments
            seg = self.traj[ignore_n_images+start:ignore_n_images+end]

            # If we are considering a molecular system, work out the COM for the starting structure
            if self.molecule:
                com_orig = np.zeros(3)
                for atom_no in self.atom_indices:
                    com_orig[:] += seg[0].positions[atom_no][:] / len(self.atom_indices)

            # For each image, calculate displacement.
            # I spent some time deciding if this should run from 0 or 1, as the displacement will be zero for 
            # t = 0, but this is a data point that needs fitting too and so should be included
            for image_no in range(0,len(seg)): 
                # This object collects the xyz displacements for all atom species in the image
                xyz_disp = np.zeros((self.no_of_types_of_atoms,3))
                
                # Calculating for each atom individually, grouping by species type (e.g. solid state)
                if not self.molecule:
                    # For each atom, work out displacement from start coordinate and collect information with like atoms
                    for atom_no in self.atom_indices:
                        sym_index = self.types_of_atoms.index(seg[image_no].symbols[atom_no])
                        xyz_disp[sym_index][:] += np.square(seg[image_no].positions[atom_no][:] - seg[0].positions[atom_no][:])
        
                else: # Calculating for group of atoms (molecule) and work out squared displacement
                    com_disp = np.zeros(3)
                    for atom_no in self.atom_indices:
                        com_disp[:] += seg[image_no].positions[atom_no][:] / len(self.atom_indices)
                    xyz_disp[0][:] += np.square(com_disp[:] - com_orig[:])

                # For each atom species or molecule, use xyz_disp to calculate the average data                      
                for sym_index in range(self.no_of_types_of_atoms):
                    # Normalise by degrees of freedom and average overall atoms for each axes over entire segment                         
                    denominator = (2*self.no_of_atoms[sym_index])
                    for xyz in range(3):
                        self.xyz_segment_ensemble_average[segment_no][sym_index][xyz][image_no] = (xyz_disp[sym_index][xyz]/denominator)

            # We've collected all the data for this entire segment, so now to fit the data.
            for sym_index in range(self.no_of_types_of_atoms):    
                self.slopes[sym_index][segment_no], self.intercepts[sym_index][segment_no] = self.__fit_data(self.timesteps[start:end], 
                                                                                                             self.xyz_segment_ensemble_average[segment_no][sym_index][:])

    def __fit_data(self, x, y):

        '''

        '''

        # Simpler implementation but disabled as fails Conda tests.
        # from scipy.stats import linregress
        # slope, intercept, r_value, p_value, std_err = linregress(x,y)
       
        # Initialise objects
        slopes = np.zeros(3)
        intercepts = np.zeros(3)

        # Convert into suitable format for lstsq
        x_edited = np.vstack([np.array(x), np.ones(len(x))]).T
        # Calculate slopes for x, y and z-axes
        for xyz in range(3):
            slopes[xyz], intercepts[xyz] = np.linalg.lstsq(x_edited, y[xyz], rcond=-1)[0]

        return slopes, intercepts

    def get_diffusion_coefficients(self, stddev=False):

        '''

        '''

        # Safety check, so we don't return garbage.
        if len(self.slopes) == 0:
            self.calculate()

        slopes = [np.mean(self.slopes[sym_index]*(0.1)) for sym_index in range(self.no_of_types_of_atoms)]
        std = [np.std(self.slopes[sym_index]*(10**-16)) for sym_index in range(self.no_of_types_of_atoms)]

        # Converted gradient from \AA^2/fs to more common units of cm^2/s => multiply by 10^-1
        # Converted std from \AA^2 to more common units of cm^2 => multiply by 10^-16
        # \AA^2 => cm^2 requires multiplying by (10^-8)^2 = 10^-16
        # fs => s requires dividing by 10^-15
        
        if stddev:
            return slopes, std
       
        return slopes

    def plot(self):

        '''
        Auto-plot of Diffusion Coefficient data
        '''

        # Moved matplotlib into the function so it is not loaded unless needed
        # Could be provided as an input variable, so user can work with it further?
        import matplotlib.pyplot as plt
        
        # Define some aesthetic variables
        color_list = plt.cm.Set3(np.linspace(0, 1, self.no_of_types_of_atoms))
        xyz_labels=['X','Y','Z']
        xyz_markers = ['o','s','^']

        # Check if we have data to plot, if not calculate it.
        if len(self.slopes) == 0:
            self.calculate()
        
        for segment_no in range(self.no_of_segments):
            start = segment_no*self.len_segments  
            end = start + self.len_segments
            label = None
            
            for sym_index in range(self.no_of_types_of_atoms): 
                for xyz in range(3):
                    if segment_no == 0:
                        label = 'Species: %s (%s)'%(self.types_of_atoms[sym_index], xyz_labels[xyz])
                    plt.scatter(self.timesteps[start:end], self.xyz_segment_ensemble_average[segment_no][sym_index][xyz],
                             color=color_list[sym_index], marker=xyz_markers[xyz], label=label, linewidth=1, edgecolor='grey')

                # Print the line of best fit for segment      
                line = np.mean(self.slopes[sym_index][segment_no])*self.timesteps[start:end]+np.mean(self.intercepts[sym_index][segment_no])
                if segment_no == 0:
                    label = 'Segment Mean : %s'%(self.types_of_atoms[sym_index])
                plt.plot(self.timesteps[start:end], line, color='C%d'%(sym_index), label=label, linestyle='--')
 
            # Plot separator at end of segment
            x_coord = self.timesteps[end-1]
            plt.plot([x_coord, x_coord],[-0.001, 1.05*np.amax(self.xyz_segment_ensemble_average)], color='grey', linestyle=":")

        # Plot the overall mean (average of slopes) for each atom species
        # This only makes sense if the data is all plotted on the same x-axis timeframe, which currently we are not - everything is plotted sequentially
        #for sym_index in range(self.no_of_types_of_atoms):
        #    line = np.mean(self.slopes[sym_index])*self.timesteps+np.mean(self.intercepts[sym_index])
        #    label ='Mean, Total : %s'%(self.types_of_atoms[sym_index])
        #    plt.plot(self.timesteps, line, color='C%d'%(sym_index), label=label, linestyle="-")

        plt.ylim(-0.001, 1.05*np.amax(self.xyz_segment_ensemble_average))
        plt.legend(loc='best')
        plt.xlabel('Time (fs)')
        plt.ylabel(r'Mean Square Displacement ($\AA^2$)')

        plt.show()

    def print_data(self):

        '''

        '''
 
        slopes, std = self.get_diffusion_coefficients(stddev=True)

        for sym_index in range(self.no_of_types_of_atoms):
            print('---')
            print(r'Species: %4s' % self.types_of_atoms[sym_index])
            print('---')
            for segment_no in range(self.no_of_segments):
                print(r'Segment   %3d:         Diffusion Coefficient = %.10f cm^2/s; Intercept = %.10f cm^2;' % 
                     (segment_no, np.mean(self.slopes[sym_index][segment_no])*(0.1), np.mean(self.intercepts[sym_index][segment_no])*(10**-16)))

        print('---')
        for sym_index in range(self.no_of_types_of_atoms):
            print('Mean Diffusion Coefficient (X, Y and Z) : %s = %.10f cm^2/s; Std. Dev. = %.10f cm^2/s' % 
                 (self.types_of_atoms[sym_index], slopes[sym_index], std[sym_index]))
        print('---')