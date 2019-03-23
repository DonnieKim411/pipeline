%{
# Optical Flow for all frames using Lucas-Kanade method
-> stimulus.MovieClip
---
orientation          : mediumblob   # mean phase angle of optical flow
magnitude            : mediumblob   # Magnitude of optical flow
%}

classdef  OpticFlowFine < dj.Imported
    methods(Access=protected)
        function makeTuples(obj,key) %create clips
            rsz = 10;
            
            % get video file
            filename = exportMovie(stimulus.MovieClip & key);
            vidReader = VideoReader(filename{1});
            
            % construct optic flow algorithm
            opticFlow = opticalFlowLK('NoiseThreshold',0.001);
            
            % initialize
            iframe = 0;
            nframes =  floor(vidReader.Duration*vidReader.FrameRate);
            sz = ceil([vidReader.Height vidReader.Width]/rsz);
            Orientation = nan(sz(1),sz(2),nframes);
            Magnitude = Orientation;
            
            % fun for each frame
            while hasFrame(vidReader)
                iframe = iframe+1;
                frame = imresize(readFrame(vidReader),sz);
                flow = estimateFlow(opticFlow,frame(:,:,1));
                Orientation(:,:,iframe) = flow.Orientation;
                Magnitude(:,:,iframe) = flow.Magnitude;
            end
            key.orientation = single(Orientation(:,:,1:iframe));
            key.magnitude =single( Magnitude(:,:,1:iframe));
            
            insert( obj, key );
        end
    end
    
    methods
        function play(self)
            %fetch stuff
            filename = exportMovie(stimulus.MovieClip & self);
            vidReader = VideoReader(filename{1});
            [Orientation,Magnitude] = fetch1(self,'orientation','magnitude');
            sz = size(Orientation);
            
            % setup figure
            figure
            set(gcf,'position',[300         200        1072         634])
            colormap gray
            [x,y] = meshgrid(1:sz(2),1:sz(1));
            
            % loop through frames
            for i = 1:sz(3)
                clf
                image(readFrame(vidReader))
                hold on
                quiver(x*10- 9,y*10 - 9,cos(Orientation(:,:,i)).*Magnitude(:,:,i),...
                    sin(Orientation(:,:,i)).*Magnitude(:,:,i))
                set(gca,'YDir','reverse')
                axis off
                axis image
                xlim([-5 sz(2)*10+5])
                ylim([-5 sz(1)*10+5])
                drawnow
            end
        end
    end
end