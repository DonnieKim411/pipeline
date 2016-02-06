%{
pre.SegmentMask (computed) # mask of a segmented cell
-> pre.Segment
mask_id                     : int # id of the mask
-----
mask_pixels                  : longblob # indices into the image in column major (Fortran) order
mask_weights                 : longblob # weights of the mask at the indices above
%}

classdef SegmentMask < dj.Relvar
    methods
        
        function makeTuples(self, key)
            switch fetch1(pre.SegmentMethod & key,'method_name')
                %% manual segmentation
                case 'manual'
                    mask = fetch1(pre.ManualSegment & key, 'mask');
                    regions = regionprops(bwlabel(mask, 4),'PixelIdxList'); %#ok<MRPBW>
                    regions =  dj.struct.rename(regions, 'PixelIdxList', 'mask_pixels');
                    tuples = arrayfun(@(i) setfield(regions(i), 'mask_id', i), 1:length(regions)); %#ok<SFLD>
                    tuples = dj.struct.join(key, tuples');
                    [tuples.mask_weights] = deal(1);
                    
                    self.insert(tuples)
                %% NMF segmentation
                case 'nmf' 
                    cfg = fetch(pre.NMFSettings & key,'*');
                    
                    nslices = fetch1(pre.ScanInfo & key, 'nslices');
                    assert(nslices==1, 'This schema only supports one slice.')

                    [um_width, um_height] = fetch1(pre.ScanInfo & key, 'um_width', 'um_height');
                    cfg.max_neurons = round((um_width * um_height)/1000^2 * cfg.density);

                    fprintf('Using max %i neurons\n',cfg.max_neurons);

                    % downsample to 2 Hz
                    stride = floor(fetch1(pre.ScanInfo & key, 'fps')/cfg.downsample_to);
                    
                    Y = squeeze(self.load_scan(key, stride));

                    A = self.run_nmf(Y, cfg);

                    for idx = 1:size(A,2)
                        key.mask_id = idx;
                        I = find( A(:,idx));
                        key.mask_pixels = I;
                        key.mask_weights = A(I, idx);
                        self.insert(key);
                    end
                otherwise
                    error 'Unknown segmentation method'
            end
            
            
        end
    end
    
    methods(Static)
        
        function A = run_nmf(Y, cfg)
        %
        % Runs the nonnegative matrix factorization algorithm on Y with configuration cfg. 
        %  
            [d1,d2, T] = size(Y);
            d = d1*d2;
            
            K = cfg.max_neurons;    % number of components to be found
            tau = cfg.tau;          % std of gaussian kernel (size of neuron)
            p = cfg.p;              % order of autoregressive system (p = 0 no dynamics, p=1 just decay, p = 2, both rise and decay)
            
            
            options = CNMFSetParms(...
                'd1',d1,'d2',d2,...
                'search_method',cfg.search_method ,'dist',cfg.dist,...
                'deconv_method',cfg.deconv_method,...
                'temporal_iter',cfg.temporal_iter,...
                'fudge_factor',cfg.fudge_factor,...
                'merge_thr',cfg.merge_thr,...
                'se', strel('disk',cfg.se,0) ...
                );
            %% Data pre-processing
            
            [P,Y] = preprocess_data(Y,p);
            
            %% fast initialization of spatial components using greedyROI and HALS
            [A,C,~,f] = initialize_components(Y,K,tau,options);  % initialize
            
            %% update spatial and temporal components
            Yr = reshape(Y,d,T);
            clear Y;
            
            % update spatial components
            [A,b] = update_spatial_components(Yr,C,f,A,P,options);
            [C,f,P,S] = update_temporal_components(Yr,A,b,C,f,P,options);
            % merge found components
            [A,C,~,~,P,S] = merge_components(Yr,A,b,C,f,P,S,options);
            [A,~,~,~,~] = order_ROIs(A,C,S,P);    % order components
           
        end
        
        
        %%------------------------------------------------------------
        function scan = load_scan(key, stride, maxT, blockSize)
        % 
        % 
        %  If maxT is specified, it loads the first maxT frames. 
        %  If blockSize is specified, the TIFF stack is loaded in chunks of blockSize. 
        %  Default is blockSize=10000. 
        %
            reader = pre.getReader(key, '~/cache');
            assert(reader.nslices == 1, 'schema only supports one slice at the moment');
            
            if nargin < 2
                stride = 1;
            end
            
            if nargin < 3
                maxT = reader.nframes;
            end
            
            if nargin < 4
                blockSize = min(maxT, 10000);
            end
        
            [r,c] = fetch1(pre.ScanInfo & key, 'px_height', 'px_width');
            
            fixRaster = get_fix_raster_fun(pre.AlignRaster & key);
            fixMotion = get_fix_motion_fun(pre.AlignMotion & key);
           
            scan = zeros(r,c, 1, maxT);
            
            h = hamming(2*stride+1);
            h = reshape(h/sum(h), 1,1,1,2*stride+1);
            pointer = 1;
            while pointer < maxT
                step =  min(blockSize, maxT-pointer+1);
                frames = pointer:pointer+step-1;
                fprintf('Reading frames %i:%i of maximally %i (video has %i frames)\n', ...
                    pointer, pointer + step - 1, maxT, reader.nframes);
                scan(:, :, 1, frames) = fixMotion(fixRaster(single(reader(:,:,:,:,frames))), frames);
                pointer = pointer + step;
            end
            scan = convn(scan, h, 'same');
            scan = scan(:,:,:,1:stride:end);
            
        end
    end
    
    
end