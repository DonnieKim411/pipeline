%{
# unit type and coordinates in x, y, z
-> reso.ScanSetUnit
---
-> `pipeline_shared`.`#mask_type`
um_x                        : smallint                      # x-coordinate of centroid in motor coordinate system
um_y                        : smallint                      # y-coordinate of centroid in motor coordinate system
um_z                        : smallint                      # z-coordinate of mask relative to surface of the cortex
px_x                        : smallint                      # x-coordinate of centroid in the frame
px_y                        : smallint                      # y-coordinate of centroid in the frame
%}


classdef ScanSetUnitInfo < dj.Computed

	methods(Access=protected)

		function makeTuples(self, key)
		%!!! compute missing fields for key here
			 self.insert(key)
		end
	end

end