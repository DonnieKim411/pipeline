%{
aodpre.ComputeTraces (computed) # traces used for spike extraction
-> aodpre.Set
-> aodpre.PreprocessMethod
-----
%}

classdef ComputeTraces < dj.Relvar & dj.AutoPopulate
    
    properties
        popRel  = aodpre.Set*aodpre.PreprocessMethod
    end
    
    methods(Access=protected)
        
        function makeTuples(self, key)
            self.insert(key)
            switch fetch1(aodpre.PreprocessMethod & key, 'preprocess_name')
                case 'raw'
                    %for regular imaging, use channel 1.  Handle ratiometric separately
                    insert(aodpre.Trace, rmfield(fetch(aodpre.Timeseries*aodpre.PreprocessMethod & key & 'channel=1', 'trace'), 'channel'))
                case '-1pc'
                    [X, keys] = fetchn(aodpre.Timeseries*aodpre.PreprocessMethod & key & 'channel=1', 'trace');
                    keys = rmfield(keys, 'channel');
                    X = double([X{:}]);
                    M = mean(X);
                    % subtract 1 principal component  (not including means)
                    X = bsxfun(@minus, X, M);
                    [U,D,V] = svds(X,1);
                    X = X - U*D*V';
                    % add the mean back
                    X = bsxfun(@plus, X, M);
                    for i=1:length(keys)
                        tuple = keys(i);
                        tuple.trace = single(X(:,i));
                        insert(aodpre.Trace,tuple);
                    end
                    
                case 'manolis'
                    [X, keys] = fetchn(aodpre.Timeseries*aodpre.PreprocessMethod & key & 'channel=1', 'trace');
                    keys = rmfield(keys, 'channel');
                    fps = fetch1(aodpre.Set & key,'sampling_rate');
                    
                    % remove baseline
                    traces = double(cell2mat(X'));
                    traces = traces - min(traces(:));
                    
                    % high-pass
                    k = hamming(round(fps/0.1)*2+1);
                    k = k/sum(k);
                    traces = traces + abs(min(traces(:)))+eps;
                    traces = traces./convmirr(traces,k)-1;
                    traces(isnan(traces)) = 0;
                    
                    % remove 1pc
                    [c, p] = princomp(traces);
                    traces = p(:,2:end)*c(:,2:end)';
                    
                    for i=1:length(keys)
                        tuple = keys(i);
                        tuple.trace = single(traces(:,i));
                        insert(aodpre.Trace,tuple);
                    end
                    
                case 'manolis_LP'
                    [X, keys] = fetchn(aodpre.Timeseries*aodpre.PreprocessMethod & key & 'channel=1', 'trace');
                    keys = rmfield(keys, 'channel');
                    fps = fetch1(aodpre.Set & key,'sampling_rate');
                    
                    % remove baseline
                    traces = double(cell2mat(X'));
                    traces = traces - min(traces(:));
                    
                    % high-pass
                    k = hamming(round(fps/0.1)*2+1);
                    k = k/sum(k);
                    traces = traces + abs(min(traces(:)))+eps;
                    traces = traces./convmirr(traces,k)-1;
                    traces(isnan(traces)) = 0;
                    
                    % low-pass
                    k = hamming(round(fps/5)*2+1);
                    k = k/sum(k);
                    traces = convmirr(traces,k);
                    
                    % remove 1pc
                    [c, p] = princomp(traces);
                    traces = p(:,2:end)*c(:,2:end)';
                    
                    for i=1:length(keys)
                        tuple = keys(i);
                        tuple.trace = single(traces(:,i));
                        insert(aodpre.Trace,tuple);
                    end
                otherwise
                    error 'unknown preprocessing method'
            end
        end
    end
    
end