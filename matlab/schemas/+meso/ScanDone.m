%{
# scans that are fully processed (updated every time a field is added)
-> meso.ScanInfo
-> `pipeline_shared`.`#segmentation_method`
-> `pipeline_shared`.`#spike_method`
%}


classdef ScanDone < dj.Computed

	methods(Access=protected)

		function makeTuples(self, key)
		%!!! compute missing fields for key here
			 %self.insert(key)
		end
	end

end